import type { MoviElement } from "movi-player/element/slim";

type GesturePlayer = {
    isAnyMenuOpen(): boolean;
    isTimelineOpen(): boolean;
    toggleTimeline(): void;
    closeSettingsMenu(): void;
    closeAllBottomMenus(): void;
    showControls(): void;
};

function touchesPicture(event: Event): boolean {
    const path = event.composedPath();
    if (
        path.some(
            (node) =>
                node instanceof Element &&
                node.matches(
                    "button,input,select,a,label,.movi-controls-bar,.movi-timeline-panel,.movi-overlay-layer",
                ),
        )
    )
        return false;
    return path.some(
        (node) =>
            node instanceof Element &&
            node.matches("canvas,video,.movi-controls-overlay"),
    );
}

/** Retain Movi's native taps, seeking and mouse gestures. Pinned 0.4.1 checks
 * for an open menu only after recognizing a double tap, so tapping outside a
 * menu can seek behind it. Consume that dismissal and its remaining tap burst.
 */
export function attachVideoGestures(player: MoviElement): () => void {
    const gestures = player as unknown as GesturePlayer;
    let consuming = false;
    let consumeUntil = 0;
    const start = (event: TouchEvent) => {
        if (!touchesPicture(event)) return;
        const chapters = gestures.isTimelineOpen();
        const menu = gestures.isAnyMenuOpen() || chapters;
        consuming = menu || performance.now() < consumeUntil;
        if (!consuming) return;
        consumeUntil = performance.now() + 450;
        if (menu) {
            gestures.closeSettingsMenu();
            gestures.closeAllBottomMenus();
            if (chapters) gestures.toggleTimeline();
            gestures.showControls();
        }
        event.stopPropagation();
    };
    const move = (event: TouchEvent) => {
        if (consuming && touchesPicture(event)) event.stopPropagation();
    };
    const end = (event: TouchEvent) => {
        if (!consuming || !touchesPicture(event)) return;
        consumeUntil = performance.now() + 450;
        event.stopPropagation();
        if (event.cancelable) event.preventDefault();
        if (!event.touches.length) consuming = false;
    };
    const ghostClick = (event: MouseEvent) => {
        const touch = event as MouseEvent & {
            pointerType?: string;
            sourceCapabilities?: { firesTouchEvents?: boolean };
        };
        if (
            performance.now() < consumeUntil &&
            (touch.pointerType === "touch" ||
                touch.sourceCapabilities?.firesTouchEvents) &&
            touchesPicture(event)
        ) {
            event.preventDefault();
            event.stopPropagation();
        }
    };
    player.addEventListener("touchstart", start, {
        capture: true,
        passive: true,
    });
    player.addEventListener("touchmove", move, {
        capture: true,
        passive: true,
    });
    player.addEventListener("touchend", end, { capture: true, passive: false });
    player.addEventListener("touchcancel", end, {
        capture: true,
        passive: false,
    });
    player.addEventListener("click", ghostClick, true);
    player.addEventListener("dblclick", ghostClick, true);
    return () => {
        player.removeEventListener("touchstart", start, true);
        player.removeEventListener("touchmove", move, true);
        player.removeEventListener("touchend", end, true);
        player.removeEventListener("touchcancel", end, true);
        player.removeEventListener("click", ghostClick, true);
        player.removeEventListener("dblclick", ghostClick, true);
    };
}
