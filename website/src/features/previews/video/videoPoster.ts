import { readBoundedResponse } from "../previewIO";
import type { MoviElement } from "movi-player/element/slim";

/** Pinned Movi's representative reset must not undo a newer user seek. */
export function guardVideoPosterSeek(player: MoviElement): () => void {
    const core = player.player;
    if (!core) return () => {};
    let pending = true;
    let superseded = false;
    const reset = core.resetClockToStartForPoster;
    const guardedReset = () => {
        pending = false;
        if (!superseded) reset.call(core);
    };
    const complete = () => {
        pending = false;
        core.off("seekcomplete", complete);
    };
    const seeking = () => {
        if (!pending) return;
        superseded = true;
        player.removeAttribute("postertime");
    };
    const play = () => {
        if (!pending) return;
        seeking();
        // Play replaces an unfinished representative seek with the real start.
        void core.seek(0).catch(() => {});
    };
    core.resetClockToStartForPoster = guardedReset;
    core.on("seekcomplete", complete);
    player.addEventListener("seeking", seeking);
    player.addEventListener("play", play);
    return () => {
        player.removeEventListener("seeking", seeking);
        player.removeEventListener("play", play);
        core.off("seekcomplete", complete);
        if (core.resetClockToStartForPoster === guardedReset)
            core.resetClockToStartForPoster = reset;
    };
}

export function videoPosterUrl(path: string, auth: string): string {
    return (
        "/api/v1/poster?" +
        new URLSearchParams({ path, ...(auth ? { auth } : {}) })
    );
}

/** A missing or unusable stored poster leaves frame decoding to the player. */
export async function loadVideoPoster(
    url: string,
    signal: AbortSignal,
): Promise<Blob | null> {
    const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(10000)]);
    try {
        const response = await fetch(url, {
            credentials: "same-origin",
            cache: "no-cache",
            headers: { Accept: "image/jpeg" },
            signal: requestSignal,
        });
        if (
            !response.ok ||
            response.status === 204 ||
            response.headers.get("content-type")?.split(";")[0].trim() !==
                "image/jpeg"
        ) {
            await response.body?.cancel();
            return null;
        }
        const bytes = await readBoundedResponse(
            response,
            1024 * 1024,
            requestSignal,
        );
        if (
            bytes.length < 5 ||
            bytes[0] !== 0xff ||
            bytes[1] !== 0xd8 ||
            bytes[2] !== 0xff ||
            bytes.at(-2) !== 0xff ||
            bytes.at(-1) !== 0xd9
        )
            return null;
        return new Blob([bytes.slice()], { type: "image/jpeg" });
    } catch {
        return null;
    }
}
