import type { MoviElement } from "movi-player/element/slim";
import { version } from "../../../../package.json";
import type { VideoBufferSnapshot } from "./AdvancedVideo";

const formats: Record<string, string> = {
    matroska: "Matroska / WebM",
    webm: "WebM",
    mov: "MP4 / QuickTime",
    mp4: "MP4",
    avi: "AVI",
    asf: "ASF",
    flv: "FLV",
    mpeg: "MPEG",
    mpegts: "MPEG-TS",
    ogg: "Ogg",
};

function knownCodec(value: string | undefined): string | undefined {
    if (!value) return;
    const name = value.toLowerCase().split(".")[0];
    return /^(?:avc[13]|hev1|hvc1|vp09|av01|mp4a|av1|h26[345]|hevc|vp[689]|mpeg[124](?:video)?|theora|vc1|wmv[123]|aac|ac3|eac3|dts|truehd|opus|vorbis|flac|mp[23]|alac|pcm_[suf][0-9]{1,2}(?:le|be))$/.test(
        name,
    )
        ? name
        : undefined;
}

export type BufferingTransition = {
    state: "entered" | "left";
    at: number;
    position?: number;
};

function seconds(value: number | undefined) {
    return value != null && Number.isFinite(value) && value >= 0
        ? value.toFixed(2) + " s"
        : "Unavailable";
}

export function playbackDiagnostics({
    player,
    compatible,
    buffer,
    lastFrame,
    buffering,
}: {
    player: MoviElement | null;
    compatible: boolean;
    buffer?: VideoBufferSnapshot;
    lastFrame?: number;
    buffering: BufferingTransition | null;
}): string {
    const build = Array.from(document.scripts)
        .map(
            (script) =>
                script
                    .getAttribute("src")
                    ?.match(
                        /^\/(?:assets|static)\/index-([a-z0-9_-]{6,24})\.js$/i,
                    )?.[1],
        )
        .find(Boolean);
    const engine = player?.player;
    const format = engine?.getMediaInfo()?.formatName?.split(",")[0];
    const tracks = engine?.trackManager;
    const health = engine?.getRenderHealth();
    const audio = engine as
        | {
              audioRenderer?: { audioContext?: { state?: string } };
          }
        | null
        | undefined;
    const audioState = audio?.audioRenderer?.audioContext?.state;
    const ranges = buffer?.ranges.slice(-8);
    const rate = player?.playbackRate;
    return [
        "TGDrive playback diagnostics",
        "Build: " + version + (build ? " (" + build + ")" : ""),
        "Mode: " +
            (compatible
                ? "Compatible (browser decoder)"
                : "Original (enhanced player)"),
        "Container: " +
            (format && Object.hasOwn(formats, format)
                ? formats[format]
                : "Unavailable"),
        "Video codec: " +
            (knownCodec(tracks?.getActiveVideoTrack()?.codec) ?? "Unavailable"),
        "Audio codec: " +
            (knownCodec(tracks?.getActiveAudioTrack()?.codec) ?? "Unavailable"),
        "Position: " + seconds(player?.currentTime),
        "Rate: " +
            (rate && Number.isFinite(rate)
                ? rate.toFixed(2) + "×"
                : "Unavailable"),
        "Last buffering transition: " +
            (buffering
                ? buffering.state +
                  " at " +
                  seconds(buffering.position) +
                  " (" +
                  seconds((performance.now() - buffering.at) / 1000) +
                  " ago)"
                : "None observed"),
        "Buffered time ranges: " +
            (ranges?.length
                ? ranges
                      .map(
                          ({ start, end }) =>
                              seconds(start) + "–" + seconds(end),
                      )
                      .join(", ")
                : "Unavailable"),
        "Last presented frame: " + seconds(lastFrame),
        ...(health && Number.isFinite(health.framesPresented)
            ? [
                  "Frames presented since seek or reset: " +
                      health.framesPresented,
              ]
            : []),
        ...(buffer?.measured
            ? [
                  "Delivery: " +
                      buffer.received +
                      " bytes received, " +
                      buffer.bytesPerSecond +
                      " bytes/s",
              ]
            : []),
        ...(buffer?.deliveryWaiting ? ["Delivery wait: Reported"] : []),
        ...(buffer?.decodeBound ? ["Decoder wait: Reported"] : []),
        ...(audioState &&
        ["running", "suspended", "closed", "interrupted"].includes(audioState)
            ? ["Audio context: " + audioState]
            : []),
    ].join("\n");
}
