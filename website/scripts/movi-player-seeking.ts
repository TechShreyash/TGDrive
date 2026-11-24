// The installed slim bundle is hash-pinned by movi-player-build.ts.
export function patchMoviSeeking(source: string): string {
    const edits = [
        // Asyncify has a single suspended stack and pending read per module.
        // Keep the result copy inside the operation, including for subtitles.
        [
            "this.module = A2, this.setupAsyncHandlers(), m.add(this);",
            "this.module = A2, this.setupAsyncHandlers(), m.add(this); serializeDemuxOperations(this);",
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
