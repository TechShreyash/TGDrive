// The installed slim bundle is hash-pinned by movi-player-build.ts.
export function patchMoviSeeking(source: string): string {
    const lowMemoryDesktop =
        "!MoviPlayer._isMobileDevice && typeof navigator !== 'undefined' && navigator.deviceMemory > 0 && navigator.deviceMemory <= 4";
    const edits = [
        // Asyncify has a single suspended stack and pending read per module.
        // Keep the result copy inside the operation, including for subtitles.
        [
            "this.module = A2, this.setupAsyncHandlers(), m.add(this);",
            "this.module = A2, this.setupAsyncHandlers(), m.add(this); serializeDemuxOperations(this);",
        ],
        // WebCodecs can decode in software on desktop too. Apply the existing
        // decoded-frame budget on desktops reporting at most 4 GiB of RAM.
        [
            "if (!I2 && (t2 || !s2)) return requested;",
            `if (!I2 && (t2 || !s2) && !(${lowMemoryDesktop})) return requested;`,
        ],
        [
            "(M2 || MoviPlayer._isMobileDevice && (this.trackManager.getActiveVideoTrack()?.width ?? 0) * (this.trackManager.getActiveVideoTrack()?.height ?? 0) < 8294400) && ((this.videoRenderer?.getQueueSize() ?? 0) + this.videoDecoder.queueSize) > N2",
            // An empty renderer may still need several input packets before a
            // reordered picture is emitted. The decoder has its own input limit.
            `(M2 || MoviPlayer._isMobileDevice && (this.trackManager.getActiveVideoTrack()?.width ?? 0) * (this.trackManager.getActiveVideoTrack()?.height ?? 0) < 8294400 || ${lowMemoryDesktop} && (this.videoRenderer?.getQueueSize() ?? 0) > 0) && ((this.videoRenderer?.getQueueSize() ?? 0) + this.videoDecoder.queueSize) > N2`,
        ],
        // The bindings notify their adapter. Forward the hint to HttpSource so
        // a search for a keyframe doesn't start downloading at every probe.
        [
            "class SourceDataAdapter {\n  source;",
            "class SourceDataAdapter {\n  source;\n  setProbeMode(enabled) { this.source.setProbeMode?.(enabled); }",
        ],
        [
            "if (!t2 && this.atomicIsStreaming() && (!this.seekHinted",
            "if (!t2 && (this._probing || this.atomicIsStreaming()) && (!this.seekHinted",
        ],
        // A newer seek can start while native I/O is suspended. Only its owner
        // may move the playback clock, clear decoder state or reposition audio.
        [
            "return this.demuxInFlight = false, this.abandonSupersededSeek(Q2);",
            "return this.abandonSupersededSeek(Q2);",
        ],
        [
            "await this.demuxer.seek(A2 + this.startTime), this.audioDemuxer) {",
            "await this.demuxer.seek(A2 + this.startTime), this.seekSessionId !== Q2) return this.abandonSupersededSeek(Q2);\n      if (this.audioDemuxer) {",
        ],
        [
            "for (; this.audioDemuxInFlight && I4++ < 200; ) await new Promise((A3) => setTimeout(A3, 5));",
            "for (; this.audioDemuxInFlight && I4++ < 200; ) await new Promise((A3) => setTimeout(A3, 5));\n        if (this.seekSessionId !== Q2) return this.abandonSupersededSeek(Q2);",
        ],
        [
            "this._splitAudioEof = false, this._lastSplitAudioPts = A2;",
            "if (this.seekSessionId !== Q2) return this.abandonSupersededSeek(Q2);\n        this._splitAudioEof = false, this._lastSplitAudioPts = A2;",
        ],
        [
            "throw this.seekingToKeyframe = false, this.seekSessionId === Q2 &&",
            "if (this.seekSessionId !== Q2) return this.abandonSupersededSeek(Q2);\n      throw this.seekingToKeyframe = false,",
        ],
        // Picture completion clears seekTargetTime independently of the audio
        // demux cursor. Keep its own lower bound until muxed audio catches up.
        [
            "this.seekTargetTime = A2 + this.startTime, this._audioReadPassStart",
            "this.seekTargetTime = A2 + this.startTime, this._rewindAudioFrom = this.seekTargetTime, this._audioReadPassStart",
        ],
    ];
    for (const [before, after] of edits) {
        if (source.split(before).length !== 2)
            throw new Error("The reviewed Movi seek path changed.");
        source = source.replace(before, after);
    }
    return (
        'import { serializeDemuxOperations } from "virtual:tgdrive-movi-demux-queue";\n' +
        source
    );
}
