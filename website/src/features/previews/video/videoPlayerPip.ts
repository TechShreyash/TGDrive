import pipStyles from "./videoPlayerPip.css?inline";
import subtitleStyles from "./MovTextRenderer.css?inline";
import { formatVideoTime } from "./videoWatchProgress";

type PipWindow = Window & typeof globalThis;
type PipOptions = { width?: number; height?: number };
type PipController = {
    requestWindow(options?: PipOptions): Promise<PipWindow>;
};
type PipPlayer = HTMLElement & {
    togglePiP(): Promise<void>;
    _pipWindow: PipWindow | null;
    currentTime: number;
    duration: number;
    paused: boolean;
    ended: boolean;
    muted: boolean;
};

function attachPipControls(player: PipPlayer, pip: PipWindow): () => void {
    const doc = pip.document;
    const sheet = new pip.CSSStyleSheet();
    sheet.replaceSync(subtitleStyles + pipStyles);
    doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet];
    const controls = doc.querySelector<HTMLElement>(".pip-controls");
    const position = doc.querySelector<HTMLElement>(".pip-progress-bar");
    const play = doc.querySelector<HTMLButtonElement>(".play-pause");
    const mute = doc.querySelector<HTMLButtonElement>(".mute-toggle");
    for (const [selector, label] of [
        [".seek-back", "Rewind 10 seconds"],
        [".seek-fwd", "Forward 10 seconds"],
        [".back-to-tab", "Back to video"],
    ]) {
        const button = doc.querySelector<HTMLButtonElement>(selector);
        button?.setAttribute("aria-label", label);
        if (button) button.title = label;
    }
    for (const button of doc.querySelectorAll<HTMLButtonElement>(".pip-btn"))
        button.type = "button";
    controls?.setAttribute("role", "group");
    controls?.setAttribute("aria-label", "Video controls");
    if (position) {
        position.tabIndex = 0;
        position.setAttribute("role", "slider");
        position.setAttribute("aria-label", "Video position");
        position.setAttribute("aria-orientation", "horizontal");
        position.setAttribute("aria-valuemin", "0");
    }
    const sync = () => {
        const playLabel = player.paused || player.ended ? "Play" : "Pause";
        play?.setAttribute("aria-label", playLabel);
        if (play) play.title = playLabel;
        const muteLabel = player.muted ? "Unmute" : "Mute";
        mute?.setAttribute("aria-label", muteLabel);
        if (mute) mute.title = muteLabel;
        if (position) {
            const duration = Number.isFinite(player.duration)
                ? player.duration
                : 0;
            const time = Math.min(
                duration,
                Math.max(0, player.currentTime || 0),
            );
            position.setAttribute("aria-valuemax", String(duration));
            position.setAttribute("aria-valuenow", String(time));
            position.setAttribute(
                "aria-valuetext",
                formatVideoTime(time) + " of " + formatVideoTime(duration),
            );
            position.setAttribute("aria-disabled", String(duration <= 0));
        }
    };
    const seek = (event: KeyboardEvent) => {
        if (!Number.isFinite(player.duration) || player.duration <= 0) return;
        let target: number;
        switch (event.key) {
            case "ArrowLeft":
            case "ArrowDown":
                target = player.currentTime - 5;
                break;
            case "ArrowRight":
            case "ArrowUp":
                target = player.currentTime + 5;
                break;
            case "PageDown":
                target = player.currentTime - 10;
                break;
            case "PageUp":
                target = player.currentTime + 10;
                break;
            case "Home":
                target = 0;
                break;
            case "End":
                target = player.duration;
                break;
            default:
                return;
        }
        event.preventDefault();
        // Movi also handles arrow keys on the child document.
        event.stopPropagation();
        player.currentTime = Math.min(player.duration, Math.max(0, target));
        sync();
    };
    const events = [
        "play",
        "pause",
        "playing",
        "ended",
        "volumechange",
        "timeupdate",
        "durationchange",
    ];
    const activateButton = (event: KeyboardEvent) => {
        if (
            (event.key === " " || event.key === "Enter") &&
            event.target instanceof pip.Element &&
            event.target.closest("button")
        )
            // Keep the native button action; skip Movi's document-wide play shortcut.
            event.stopPropagation();
    };
    for (const event of events) player.addEventListener(event, sync);
    position?.addEventListener("keydown", seek);
    doc.addEventListener("keydown", activateButton, true);
    sync();
    const cleanup = () => {
        for (const event of events) player.removeEventListener(event, sync);
        position?.removeEventListener("keydown", seek);
        doc.removeEventListener("keydown", activateButton, true);
        pip.removeEventListener("pagehide", closed);
    };
    const closed = () => {
        cleanup();
        if (player.isConnected)
            requestAnimationFrame(() => player.focus({ preventScroll: true }));
    };
    pip.addEventListener("pagehide", closed, { once: true });
    return cleanup;
}

/** Adapt the pinned Movi document-PiP surface to strict CSP and keyboard use.
 * Install on each fresh player and dispose before removing that player.
 */
export function installVideoPlayerPip(element: HTMLElement): () => void {
    const player = element as PipPlayer;
    const controller = (
        window as unknown as { documentPictureInPicture?: PipController }
    ).documentPictureInPicture;
    if (!controller || typeof player.togglePiP !== "function") return () => {};
    const original = player.togglePiP;
    let detachControls: (() => void) | undefined;
    let restoreRequest: (() => void) | undefined;
    let disposed = false;
    let inFlight: Promise<void> | undefined;
    const runToggle = async () => {
        if (disposed) return;
        const previousWindow = player._pipWindow;
        let restoreHead: (() => void) | undefined;
        const originalRequest = controller.requestWindow;
        const requestWindow = async (options?: PipOptions) => {
            // Call before awaiting to preserve the user's activation. Returning the
            // window only after adaptation precedes Movi's own await continuation;
            // Chromium's enter event can arrive after its styles were inserted.
            const pip = await originalRequest.call(controller, options);
            if (disposed) {
                pip.close();
                throw new DOMException(
                    "The video preview was closed.",
                    "AbortError",
                );
            }
            const doc = pip.document;
            const append = doc.head.appendChild;
            // Construct sheets in the child realm. Inline <style> insertion would be
            // blocked by the inherited style-src policy, including subtitle styles.
            doc.head.appendChild = function <T extends Node>(node: T): T {
                if (node instanceof pip.HTMLStyleElement) {
                    const sheet = new pip.CSSStyleSheet();
                    sheet.replaceSync(
                        (node.textContent || "").replace(/@import[^;]+;/g, ""),
                    );
                    doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet];
                    return node;
                }
                return append.call(this, node) as T;
            };
            restoreHead = () => {
                doc.head.appendChild = append;
            };
            return pip;
        };
        const restore = () => {
            if (controller.requestWindow === requestWindow)
                controller.requestWindow = originalRequest;
        };
        controller.requestWindow = requestWindow;
        restoreRequest = restore;
        try {
            await original.call(player);
        } finally {
            restore();
            if (restoreRequest === restore) restoreRequest = undefined;
            restoreHead?.();
            detachControls?.();
            detachControls = undefined;
            if (!disposed && player._pipWindow && !player._pipWindow.closed)
                detachControls = attachPipControls(player, player._pipWindow);
            else if (!disposed && previousWindow && player.isConnected)
                requestAnimationFrame(() =>
                    player.focus({ preventScroll: true }),
                );
        }
    };
    const toggle = () => {
        if (inFlight) return inFlight;
        inFlight = runToggle().finally(() => {
            inFlight = undefined;
        });
        return inFlight;
    };
    player.togglePiP = toggle;
    return () => {
        disposed = true;
        restoreRequest?.();
        restoreRequest = undefined;
        detachControls?.();
        if (player.togglePiP === toggle) player.togglePiP = original;
    };
}
