import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { normalizePath, type Plugin } from "vite";
import { patchMoviSource, VERSION } from "./patch-movi-player.js";
import { patchDurationRescan } from "./movi-player-duration.ts";

const SLIM_MODULE = "movi-player/element/slim";
const INSTALLED_VERSION = "0.4.1-tgdrive.3";
const INSTALLED_SHA256 =
    "f14123275c415393e88a5cfed2c529c23c6d0a5ed66d56d8de5bd0c4ff8fb59f";

/** Apply the reviewed recipe in memory; the locked npm package stays untouched. */
export function moviPlayerBuild() {
    const entry = normalizePath(
        realpathSync(fileURLToPath(import.meta.resolve(SLIM_MODULE))),
    );
    const { version } = JSON.parse(
        readFileSync(join(dirname(entry), "..", "package.json"), "utf8"),
    ) as { version: unknown };
    if (version !== INSTALLED_VERSION || basename(entry) !== "element.slim.js")
        throw new Error(
            `Review the Movi build patch before changing ${INSTALLED_VERSION}.`,
        );

    return {
        name: "tgdrive-movi-player-build",
        enforce: "pre",
        config() {
            return { optimizeDeps: { exclude: [SLIM_MODULE] } };
        },
        resolveId(id: string) {
            if (id === SLIM_MODULE) return entry;
            return null;
        },
        transform(code: string, id: string) {
            if (id.startsWith("\0")) return null;
            const [requestPath, search] = id.split("?", 2);
            const query = new URLSearchParams(search?.split("#", 1)[0]);
            if (query.has("raw") || query.has("url")) return null;
            let path = normalizePath(
                requestPath.split("#", 1)[0].replace(/^\/@fs(?=\/)/, ""),
            );
            if (path !== entry) {
                if (basename(path) !== "element.slim.js") return null;
                try {
                    path = normalizePath(realpathSync(path));
                } catch {
                    return null;
                }
            }
            if (path !== entry) return null;
            if (
                createHash("sha256").update(code).digest("hex") !==
                INSTALLED_SHA256
            )
                throw new Error(
                    "The installed Movi slim source differs from the reviewed package. Run npm ci; review any viewer upgrade before building.",
                );
            const patched = patchDurationRescan(patchMoviSource(code, VERSION));
            // A sparse picture can be decoded ahead of the playback clock.
            const audioSchedule =
                "this.audioRenderer.getMaxScheduledMediaTime() < I3 - 0.1";
            if (patched.split(audioSchedule).length !== 2)
                throw new Error("The reviewed Movi audio sync anchor changed.");
            // Low-frame-rate tracks hold each picture for a full frame interval.
            const videoLag = /\bMoviPlayer\.VIDEO_LAG_S\b/g;
            if (patched.match(videoLag)?.length !== 2)
                throw new Error(
                    "The reviewed Movi picture sync anchors changed.",
                );
            const resumeTrim =
                "this.videoRenderer.dropStaleFrames(this.clock.getTime(), 0.2)";
            if (patched.split(resumeTrim).length !== 2)
                throw new Error(
                    "The reviewed Movi refill trim anchor changed.",
                );
            const audioClock =
                "this.firstBufferMediaTime + Math.max(0, A2 * this._playbackRate)";
            if (patched.split(audioClock).length !== 3)
                throw new Error(
                    "The reviewed Movi audio clock anchors changed.",
                );
            const pictureClockTolerance = "B2 ? 0.05 : 0.15, 0.15 * C3";
            if (patched.split(pictureClockTolerance).length !== 2)
                throw new Error(
                    "The reviewed Movi picture clock anchor changed.",
                );
            const stretchDelay =
                "(this.preservePitch && this.signalsmith && this.signalsmithSampleRate > 0 " +
                "&& Math.abs(this._playbackRate - 1) > 0.01 " +
                "? (this.signalsmith.mod._movi_stretch_input_latency(this.signalsmith.handle) " +
                "+ this._playbackRate * this.signalsmith.mod._movi_stretch_output_latency(this.signalsmith.handle)) " +
                "/ this.signalsmithSampleRate : 0)";
            return {
                code: patched
                    .replaceAll(
                        audioClock,
                        // The stretcher reports input and output delay in frames.
                        // Convert both to source-media time before comparing clocks.
                        "this.firstBufferMediaTime + Math.max(0, A2 * this._playbackRate - " +
                            stretchDelay +
                            ")",
                    )
                    .replace(
                        pictureClockTolerance,
                        "B2 ? 0.05 : Math.abs(this.playbackRate - 1) > 0.01 ? 0.04 : 0.15, 0.15 * C3",
                    )
                    .replace(
                        resumeTrim,
                        // A bound refill can reanchor audio behind the paused wall
                        // clock. Keep the next pictures after the last painted one.
                        "this.videoRenderer.dropStaleFrames(" +
                            "this._resumeToPlayPending && this.getBindAV() && this.videoRenderer.getLastPresentedTime() >= 0 " +
                            "? Math.min(this.clock.getTime(), this.videoRenderer.getLastPresentedTime() + 0.2) " +
                            ": this.clock.getTime(), 0.2)",
                    )
                    .replace(
                        audioSchedule,
                        "this.audioRenderer.getMaxScheduledMediaTime() < Math.min(I3, this.clock.getTime()) - 0.1",
                    )
                    .replaceAll(
                        videoLag,
                        "Math.max(MoviPlayer.VIDEO_LAG_S, 1 / (this.trackManager.getActiveVideoTrack()?.frameRate || this.mediaInfo?.videoFrameRate || 24))",
                    ),
                map: null,
            };
        },
    } satisfies Plugin;
}
