// The installed slim bundle is hash-pinned by movi-player-build.ts.
export function patchMoviSeeking(source: string): string {
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
            "if (!I2 && (t2 || !s2) && !(navigator.deviceMemory > 0 && navigator.deviceMemory <= 4)) return requested;",
        ],
        [
            "(M2 || MoviPlayer._isMobileDevice && (this.trackManager.getActiveVideoTrack()?.width ?? 0) * (this.trackManager.getActiveVideoTrack()?.height ?? 0) < 8294400) && ((this.videoRenderer?.getQueueSize() ?? 0) + this.videoDecoder.queueSize) > N2",
            // An empty renderer may still need several input packets before a
            // reordered picture is emitted. The decoder has its own input limit.
            "(M2 || MoviPlayer._isMobileDevice && (this.trackManager.getActiveVideoTrack()?.width ?? 0) * (this.trackManager.getActiveVideoTrack()?.height ?? 0) < 8294400 || navigator.deviceMemory > 0 && navigator.deviceMemory <= 4 && (this.videoRenderer?.getQueueSize() ?? 0) > 0) && ((this.videoRenderer?.getQueueSize() ?? 0) + this.videoDecoder.queueSize) > N2",
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
