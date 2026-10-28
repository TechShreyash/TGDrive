import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { HARDENING_PATCHES } from "./movi-player-hardening.js";
import { DECODING_PATCHES } from "./movi-player-decoding.js";
import { PREVIEW_CACHE_PATCHES } from "./movi-player-preview-cache.js";

// Movi 0.4.1 can resume a seek on video alone on high-latency audio outputs.
// Late audio then pulls playback back to the requested seek position. Reuse
// its coordinated buffering path until playable sound is actually ready.
// Configure the HTTP ceiling before load and preserve retained bytes during
// growth. Video-only input must also obey video decoder backpressure.
export const VERSION = "0.4.1";
export const ORIGINAL_SHA256 =
    "834efaa395f82503fdea3538e03c2ad83f01a7fc409ca93d93dea500affa63c7";
export const BEFORE = "if (this.needsSeekResumeQueue()) return";
export const AFTER =
    "if (this.needsSeekResumeQueue() || (!this.audioRenderer.isDroppingAudio() && !this.noSoundLeftToWaitFor() && this.audioSideStillPriming())) return";
export const VIDEO_ONLY_BEFORE =
    "const Y2 = !this.disableAudio && !this.audioDemuxer,";
export const VIDEO_ONLY_AFTER =
    "const Y2 = !this.disableAudio && !this.audioDemuxer && !!this.trackManager.getActiveAudioTrack(),";
export const CACHE_BEFORE = 'cache: { type: "lru", maxSizeMB: 520 }';
export const CACHE_AFTER =
    'cache: { type: "lru", maxSizeMB: this._bufferSize > 0 ? this._bufferSize : 520 }';
export const SIZE_BEFORE =
    "C2 = I2 ? this.size : Math.floor(0.08 * this.size);";
export const SIZE_AFTER = "C2 = Math.min(this.size, A2, 33554432);";
export const WINDOW_LOG_BEFORE =
    'I2 ? "(full file cache)" : "(8% sliding window)"';
export const WINDOW_LOG_AFTER =
    'this.bufferSize >= this.size ? "(full file cache)" : "(adaptive sliding window)"';
export const WINDOW_BEFORE =
    "g2 = this.size <= I2 ? this.size : Math.floor(0.08 * this.size);";
export const WINDOW_AFTER = "g2 = Math.min(this.size, I2, 33554432);";
export const RESIZE_BEFORE = `  resizeBuffer(A2) {
    const I2 = 1024 * this.maxBufferSizeMB * 1024, C2 = Math.max(EA, Math.min(I2, A2));
    if (this.bufferSize !== C2 || !this.sharedBuffer && !this.fallbackBuffer) {
      this.bufferSize = C2;
      try {
        "undefined" != typeof SharedArrayBuffer && crossOriginIsolated ? (this.sharedBuffer = new SharedArrayBuffer(24 + this.bufferSize), this.headerView = new Int32Array(this.sharedBuffer, 0, 6), this.dataView = new Uint8Array(this.sharedBuffer, 24, this.bufferSize), this.useSharedBuffer = true, g.info(QA, \`Using SharedArrayBuffer for zero-copy streaming (\${(this.bufferSize / 1024 / 1024).toFixed(2)} MB)\`)) : (this.fallbackBuffer = new Uint8Array(this.bufferSize), g.info(QA, \`Using standard ArrayBuffer (\${(this.bufferSize / 1024 / 1024).toFixed(2)} MB)\`));
      } catch {
        this.fallbackBuffer = new Uint8Array(this.bufferSize), g.warn(QA, \`SharedArrayBuffer init failed, using fallback (\${(this.bufferSize / 1024 / 1024).toFixed(2)} MB)\`);
      }
    }
  }`;
export const RESIZE_AFTER = `  allocationLimitBytes = Infinity;
  resizeBuffer(requested) {
    const old = this.useSharedBuffer ? this.dataView : this.fallbackBuffer;
    const capacity = Math.max(EA, Math.min(1024 * this.maxBufferSizeMB * 1024,
      this.allocationLimitBytes, requested));
    const written = old ? this.atomicGetWritePos() : 0;
    if (old && (capacity === old.length || written > 0 && capacity < old.length)) return;
    if (this.headerView && Atomics.load(this.headerView, 3) !== 0) return;
    const start = old ? this.atomicGetBufferStart() : 0;
    const streaming = old ? this.atomicIsStreaming() : false;
    const header = this.headerView?.slice();
    let shared = null, view;
    try {
      if (typeof SharedArrayBuffer !== "undefined" && crossOriginIsolated) {
        shared = new SharedArrayBuffer(24 + capacity);
        view = new Uint8Array(shared, 24, capacity);
      } else view = new Uint8Array(capacity);
    } catch {
      if (old) {
        this.allocationLimitBytes = old.length;
        return;
      }
      view = new Uint8Array(capacity);
    }
    if (old && written > 0) view.set(old.subarray(0, written));
    this.bufferSize = capacity;
    this.sharedBuffer = shared;
    this.headerView = shared ? new Int32Array(shared, 0, 6) : null;
    this.dataView = shared ? view : null;
    this.fallbackBuffer = shared ? null : view;
    this.useSharedBuffer = !!shared;
    if (this.headerView && header) this.headerView.set(header);
    this.atomicSetBufferStart(start);
    this.atomicSetWritePos(written);
    this.atomicSetStreaming(streaming);
  }`;
// Keep compressed read-ahead independent of decoder admission. A time goal
// avoids treating an identical byte count as enough for every video bitrate.
export const GATE_BEFORE = `  async awaitPrefetchGate() {
    for (; this._prefetchThrottled && this.atomicIsStreaming(); ) await new Promise((A2) => setTimeout(A2, 100));
  }`;
export const GATE_AFTER = `  playbackAheadBytes = Infinity;
  playbackPrefetchHeld = false;
  setPlaybackBufferGoal(duration, bitRate = 0) {
    if (!(Number.isFinite(duration) && duration > 0 &&
        Number.isFinite(this.size) && this.size > 0)) return;
    // Demux cursor jumps include paused reads and seeks, not playback demand.
    const rate = Math.max(this.size / duration,
      Number.isFinite(bitRate) && bitRate > 0 ? bitRate / 8 : 0);
    const ceiling = Math.min(1024 * this.maxBufferSizeMB * 1024,
      this.allocationLimitBytes ?? Infinity);
    const shortClip = duration <= 120 && this.size <= ceiling;
    const wanted = shortClip ? this.size : Math.min(this.size, ceiling,
      Math.max(33554432, Math.ceil(90 * rate / 0.8 / 8388608) * 8388608));
    // Non-range readers hold their initial storage for sequential writes.
    if (!this.rangeUnsupported && wanted > this.bufferSize) this.resizeBuffer(wanted);
    this.playbackAheadBytes = shortClip && this.bufferSize >= this.size
      ? this.size : Math.max(EA, Math.min(0.8 * this.bufferSize, 90 * rate));
  }
  async awaitPrefetchGate() {
    while (this.atomicIsStreaming() && !this.closed) {
      // A distant seek starts a new source window before the demuxer can
      // finish its first read and advance position. Skipped bytes are not
      // buffered data; counting that gap deadlocks the new window at zero.
      const consumed = Math.max(this.atomicGetBufferStart(), this.getPosition());
      const ahead = Math.max(0, this.bufferEnd - consumed);
      if (ahead >= this.playbackAheadBytes) this.playbackPrefetchHeld = true;
      else if (ahead < 0.75 * this.playbackAheadBytes)
        this.playbackPrefetchHeld = false;
      // A pending demux read must complete before position can advance.
      const readerNeedsData = [...(this.pendingReadEnds || [])].some((read) =>
        read.generation === this.streamGeneration && read.end > this.bufferEnd);
      if (!this._prefetchThrottled &&
          (readerNeedsData || !this.playbackPrefetchHeld)) return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }`;
export const WAIT_BEFORE = `  async waitForData(A2, I2, C2 = 3e4) {`;
export const WAIT_AFTER = `  pendingReadEnds = new Set();
  async waitForData(offset, length, timeout = 3e4) {
    const read = {end: offset + length, generation: this.streamGeneration};
    this.pendingReadEnds.add(read);
    try {
      return await this.waitForDataUnchecked(offset, length, timeout);
    } finally {
      this.pendingReadEnds.delete(read);
    }
  }
  async waitForDataUnchecked(A2, I2, C2 = 3e4) {`;
export const GOAL_BEFORE = `    const A2 = this.mediaInfo.duration;
    if (A2 <= 0) return 0;`;
export const GOAL_AFTER = `    const A2 = this.mediaInfo.duration;
    if (A2 <= 0) return 0;
    this.source.setPlaybackBufferGoal?.(A2, this.mediaInfo.bitRate || 0);`;
export const RANGE_BEFORE = `C3 = Math.min(g2, I3 ? 8388608 : this.firstRangeBytes);`;
export const RANGE_AFTER = `C3 = Math.min(g2, I3 ? 8388608 : this.firstRangeBytes,
        Number.isFinite(this.playbackAheadBytes)
          ? Math.max(262144, Math.ceil(this.playbackAheadBytes / 12)) : Infinity);`;
export const WRITER_BEFORE = `      const w2 = this.getBuffer();
      let R2, y2, c2;`;
export const WRITER_AFTER = `      let w2 = this.getBuffer();
      let R2, y2, c2;`;
export const REFRESH_BEFORE = `        const { done: B3, value: Q3 } = await G2.read();
        if (B3) {`;
export const REFRESH_AFTER = `        const { done: B3, value: Q3 } = await G2.read();
        w2 = this.getBuffer();
        if (B3) {`;
// EOF must drain queued HEVC frames. Seek flush intentionally recreates that
// decoder, which drops its output and is unsuitable for the end-of-file path.
export const DRAIN_BEFORE = `  async flush() {
    if (this.openGopErrorCount = 0, this.justFlushed = true,`;
export const DRAIN_AFTER = `  _drainGeneration = 0;
  async flush(drain = false) {
    const generation = ++this._drainGeneration;
    if (drain && this.decoder && !this.useSoftware && !this.swDecoder) {
      const decoder = this.decoder;
      const queued = decoder.decodeQueueSize || 0;
      const timeout = Math.min(30000, Math.max(1000, 60 * queued));
      let timer;
      try {
        await Promise.race([decoder.flush(), new Promise((resolve, reject) => {
          timer = setTimeout(() => reject(new Error(
            "EOF decoder drain timed out after " + timeout + "ms")), timeout);
        })]);
      } catch (error) {
        // A newer seek, configuration or reset owns the decoder now.
        if (this._drainGeneration === generation && this.decoder === decoder) {
          try {
            decoder.reset();
            if (this.lastConfig) decoder.configure(this.lastConfig);
          } catch {}
        }
        throw error;
      } finally {
        clearTimeout(timer);
      }
      return;
    }
    if (this.openGopErrorCount = 0, this.justFlushed = true,`;
export const EOF_BEFORE = `        const A4 = ++this._eofFlushSeq;
        this.videoDecoder.flush().catch(() => {`;
export const EOF_AFTER = `        const A4 = ++this._eofFlushSeq;
        this.videoDecoder.flush(true).catch(() => {`;
export const EOF_SETTLED_BEFORE = `          this._eofFlushSettledSeq = A4;`;
export const EOF_SETTLED_AFTER = `          if (A4 === this._eofFlushSeq) this._eofFlushSettledSeq = A4;`;
export const DRAIN_RESET_BEFORE = `  reset() {
    if (this.openGopErrorCount = 0, this.swDecoder`;
export const DRAIN_RESET_AFTER = `  reset() {
    ++this._drainGeneration;
    if (this.openGopErrorCount = 0, this.swDecoder`;
export const DRAIN_CONFIG_BEFORE = `  async configure(A2, I2, C2 = 0) {
    if (this.currentTrack = A2,`;
export const DRAIN_CONFIG_AFTER = `  async configure(A2, I2, C2 = 0) {
    ++this._drainGeneration;
    if (this.currentTrack = A2,`;
export const TIMELINE_BEFORE = `    if (this._timelineGenerating) return;
    this.player.setPreviewsEnabled(true);`;
export const TIMELINE_AFTER = `    if (this._timelineGenerating) return;
    this.player.setPreviewsEnabled(this._thumb || !!this._storyboard);`;
export const CHAPTER_PREVIEW_BEFORE = `const C3 = h2[A3], B3 = C3.image ? null : await this.player.getPreviewFrame(C3.start, null, true);`;
export const CHAPTER_PREVIEW_AFTER = `const C3 = h2[A3], B3 = null;`;
export const PREVIEW_WARM_BEFORE = `  schedulePreviewWarm(A2) {`;
export const PREVIEW_WARM_AFTER = `  schedulePreviewWarm(A2) {
    return; // TGDrive initializes previews only when requested by the viewer.`;
export const EMPTY_TIMELINE_BEFORE = `        C2 && (C2.textContent = "Timeline"), g2.textContent = "Generating...";`;
export const EMPTY_TIMELINE_AFTER = `        if (!(this._thumb || this._storyboard)) {
          if (C2) C2.textContent = "Chapters";
          g2.textContent = "No chapters";
          this._timelineComplete = true;
          return;
        }
        C2 && (C2.textContent = "Timeline"), g2.textContent = "Generating...";`;
export const SUBTITLE_DISCONNECT_BEFORE = `    this.player && (this.player.destroy(), this.player = null);
  }
  attributeChangedCallback(A2, I2, C2) {`;
export const SUBTITLE_DISCONNECT_AFTER = `    this.player && (this.player.destroy(), this.player = null);
    for (const A2 of this._pickedSubtitleUrls) URL.revokeObjectURL(A2);
    this._pickedSubtitleUrls = [];
  }
  attributeChangedCallback(A2, I2, C2) {`;
export const PATCHES = [
    [BEFORE, AFTER],
    [VIDEO_ONLY_BEFORE, VIDEO_ONLY_AFTER],
    [CACHE_BEFORE, CACHE_AFTER],
    [SIZE_BEFORE, SIZE_AFTER],
    [WINDOW_LOG_BEFORE, WINDOW_LOG_AFTER],
    [WINDOW_BEFORE, WINDOW_AFTER],
    [RESIZE_BEFORE, RESIZE_AFTER],
    [GATE_BEFORE, GATE_AFTER],
    [WAIT_BEFORE, WAIT_AFTER],
    [GOAL_BEFORE, GOAL_AFTER],
    [RANGE_BEFORE, RANGE_AFTER],
    [WRITER_BEFORE, WRITER_AFTER],
    [REFRESH_BEFORE, REFRESH_AFTER],
    [DRAIN_BEFORE, DRAIN_AFTER],
    [DRAIN_RESET_BEFORE, DRAIN_RESET_AFTER],
    [DRAIN_CONFIG_BEFORE, DRAIN_CONFIG_AFTER],
    [EOF_BEFORE, EOF_AFTER],
    [EOF_SETTLED_BEFORE, EOF_SETTLED_AFTER],
    [TIMELINE_BEFORE, TIMELINE_AFTER],
    [CHAPTER_PREVIEW_BEFORE, CHAPTER_PREVIEW_AFTER],
    [EMPTY_TIMELINE_BEFORE, EMPTY_TIMELINE_AFTER],
    [SUBTITLE_DISCONNECT_BEFORE, SUBTITLE_DISCONNECT_AFTER],
    [PREVIEW_WARM_BEFORE, PREVIEW_WARM_AFTER],
    ...HARDENING_PATCHES,
    ...DECODING_PATCHES,
    ...PREVIEW_CACHE_PATCHES,
];
export const NOTICE =
    "/*! Modified by TGDrive (2026-10-20): coordinate seek resume with playable audio. See third-party/movi-player-TGDrive-patch-NOTICE.txt. */\n";
export const digest = (source) =>
    createHash("sha256").update(source).digest("hex");

export function patchMoviSource(source, version) {
    if (version !== VERSION)
        throw new Error(
            `TGDrive's seek fix requires movi-player ${VERSION}; found ${version}. Review the upstream fix before updating the pinned player.`,
        );
    const marked = source.startsWith(NOTICE);
    let original = marked ? source.slice(NOTICE.length) : source;
    for (const [before, after] of [...PATCHES].reverse())
        original = original.replace(after, before);
    if (
        digest(original) !== ORIGINAL_SHA256 ||
        PATCHES.some(([before]) => original.split(before).length !== 2)
    )
        throw new Error(
            marked
                ? "TGDrive's installed Movi seek patch was modified. Run npm ci and rebuild."
                : "The pinned Movi bundle does not match TGDrive's reviewed seek fix. Run npm ci; do not apply the patch to an unknown bundle.",
        );
    let patched = original;
    for (const [before, after] of PATCHES)
        patched = patched.replace(before, after);
    return NOTICE + patched;
}

export async function patchInstalledMovi() {
    const root = new URL("../", import.meta.url);
    const manifest = JSON.parse(
        await readFile(
            new URL("node_modules/movi-player/package.json", root),
            "utf8",
        ),
    );
    const bundle = new URL(
        "node_modules/movi-player/dist/element.slim.js",
        root,
    );
    const source = await readFile(bundle, "utf8");
    const patched = patchMoviSource(source, manifest.version);
    if (patched !== source) await writeFile(bundle, patched);
    console.info(
        `[TGDrive] Movi ${VERSION} seek, source-window and decoder-backpressure fixes verified.`,
    );
}

if (
    process.argv[1] &&
    resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
    await patchInstalledMovi();
}
