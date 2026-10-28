import type { MoviElement } from "movi-player/element/slim";

type HorizontalBox = { left: number; right: number; width: number };

/** Include one neighbouring card, rather than decoding the whole chapter list. */
export function chapterPreviewInView(
    card: HorizontalBox,
    strip: HorizontalBox,
) {
    return (
        card.width > 0 &&
        strip.width > 0 &&
        card.right > strip.left - card.width &&
        card.left < strip.right + card.width
    );
}

type PreviewCore = {
    getState(): string;
    getPreviewFrame(
        time: number,
        view?: null,
        queue?: boolean,
    ): Promise<Blob | null>;
    stopPreviewFill(): void;
    isPreviewGenerating: boolean;
};
type PreviewPlayer = HTMLElement & {
    player: PreviewCore | null;
    isTimelineOpen(): boolean;
    _currentManualRotation: number;
    syncThumbnailRotation(degrees: number): void;
};

/** Use Movi's shared keyframe decoder/cache; chapters yield to playback and hover. */
export function attachVideoPreviews(element: MoviElement): () => void {
    const player = element as unknown as PreviewPlayer;
    const document = player.ownerDocument;
    const shadow = player.shadowRoot;
    const panel = shadow?.querySelector<HTMLElement>(".movi-timeline-panel");
    const strip = shadow?.querySelector<HTMLElement>(".movi-timeline-strip");
    const progress = shadow?.querySelector<HTMLElement>(".movi-progress-bar");
    if (!shadow || !panel || !strip) return () => {};
    // Only the real buffer should look like loaded coverage on the seek bar.
    const hoverStyle = new CSSStyleSheet();
    hoverStyle.replaceSync(
        ":host{--movi-progress-hover-color:transparent!important}",
    );
    shadow.adoptedStyleSheets = [...shadow.adoptedStyleSheets, hoverStyle];

    let disposed = false;
    let hovering = false;
    let hoverPosition: { clientX: number; clientY: number } | null = null;
    let inFlight = false;
    let timer: number | undefined;
    let failed = new WeakSet<HTMLElement>();
    const images = new Map<HTMLElement, string>();
    const clearTimer = () => {
        clearTimeout(timer);
        timer = undefined;
    };
    const removeImage = (card: HTMLElement, url: string) => {
        card.querySelector("img[data-tgdrive-preview]")?.remove();
        card.classList.add("movi-timeline-noframe");
        URL.revokeObjectURL(url);
        images.delete(card);
    };
    const schedule = () => {
        clearTimer();
        if (
            !disposed &&
            !document.hidden &&
            !inFlight &&
            !hovering &&
            player.isTimelineOpen()
        )
            timer = window.setTimeout(() => void fillNext(), 200);
    };
    const fillNext = async () => {
        timer = undefined;
        const core = player.player;
        if (
            disposed ||
            document.hidden ||
            hovering ||
            !core ||
            !player.isTimelineOpen()
        )
            return;
        if (
            !["ready", "paused", "playing", "ended"].includes(
                core.getState(),
            ) ||
            core.isPreviewGenerating
        ) {
            schedule();
            return;
        }
        const bounds = strip.getBoundingClientRect();
        const visible = {
            left: Math.max(0, bounds.left),
            right: Math.min(window.innerWidth, bounds.right),
            width:
                Math.min(window.innerWidth, bounds.right) -
                Math.max(0, bounds.left),
        };
        const inView = (card: HTMLElement) =>
            chapterPreviewInView(card.getBoundingClientRect(), visible);
        for (const [card, url] of images)
            if (!strip.contains(card) || !inView(card)) removeImage(card, url);

        const card = Array.from(
            strip.querySelectorAll<HTMLElement>(".movi-timeline-chapter"),
        ).find(
            (item) =>
                !item.querySelector("img") && !failed.has(item) && inView(item),
        );
        if (!card) {
            core.stopPreviewFill();
            return;
        }
        const time = Number(card.dataset.start);
        if (!Number.isFinite(time) || time < 0) return;
        inFlight = true;
        try {
            const blob = await core.getPreviewFrame(time, null, false);
            if (
                disposed ||
                document.hidden ||
                player.player !== core ||
                !strip.contains(card) ||
                !player.isTimelineOpen() ||
                !inView(card)
            )
                return;
            if (!blob) {
                failed.add(card);
                return;
            }
            const url = URL.createObjectURL(blob);
            const image = document.createElement("img");
            image.alt = ""; // The native title/time already names this chapter.
            image.dataset.tgdrivePreview = "";
            image.addEventListener(
                "load",
                () => {
                    if (!disposed && images.get(card) === url) {
                        card.classList.remove("movi-timeline-noframe");
                        player.syncThumbnailRotation(
                            player._currentManualRotation,
                        );
                        schedule();
                    }
                },
                { once: true },
            );
            image.addEventListener(
                "error",
                () => {
                    if (images.get(card) === url) {
                        failed.add(card);
                        removeImage(card, url);
                    }
                },
                { once: true },
            );
            images.set(card, url);
            image.src = url;
            card.prepend(image);
        } catch {
            if (!disposed) failed.add(card);
        } finally {
            inFlight = false;
            if (!disposed && !hovering) core.stopPreviewFill();
            // A stationary hover refused by the busy chapter decoder needs one retry.
            if (
                !disposed &&
                !document.hidden &&
                hovering &&
                hoverPosition &&
                progress?.isConnected
            )
                progress.dispatchEvent(
                    new MouseEvent("mousemove", hoverPosition),
                );
            schedule();
        }
    };
    const refresh = () => {
        if (document.hidden || !player.isTimelineOpen()) {
            clearTimer();
            failed = new WeakSet();
            if (document.hidden) {
                hovering = false;
                hoverPosition = null;
            }
            if (document.hidden || !hovering) player.player?.stopPreviewFill();
        } else {
            hovering = false;
            hoverPosition = null;
            schedule();
        }
    };
    const enter = () => {
        if (player.isTimelineOpen()) return;
        hovering = true;
        clearTimer();
    };
    const leave = () => {
        hovering = false;
        hoverPosition = null;
        schedule();
    };
    const moved = (event: MouseEvent) => {
        // An open chapter panel hides hover previews, so idle movement must not decode them.
        if (player.isTimelineOpen() && event.buttons === 0) {
            event.stopImmediatePropagation();
            hoverPosition = null;
            if (hovering) {
                hovering = false;
                schedule();
            }
            return;
        }
        hovering = true;
        clearTimer();
        hoverPosition = { clientX: event.clientX, clientY: event.clientY };
    };
    const reset = () => {
        clearTimer();
        for (const [card, url] of images) removeImage(card, url);
        failed = new WeakSet();
    };
    const observer = new MutationObserver(refresh);
    observer.observe(panel, { attributes: true, attributeFilter: ["style"] });
    observer.observe(strip, { childList: true });
    const resize = new ResizeObserver(schedule);
    resize.observe(strip);
    strip.addEventListener("scroll", schedule, { passive: true });
    progress?.addEventListener("pointerenter", enter);
    progress?.addEventListener("pointerleave", leave);
    progress?.addEventListener("mousemove", moved, true);
    for (const event of ["statechange", "loadeddata"])
        player.addEventListener(event, refresh);
    document.addEventListener("visibilitychange", refresh);
    player.addEventListener("loadstart", reset);
    return () => {
        disposed = true;
        reset();
        observer.disconnect();
        resize.disconnect();
        strip.removeEventListener("scroll", schedule);
        progress?.removeEventListener("pointerenter", enter);
        progress?.removeEventListener("pointerleave", leave);
        progress?.removeEventListener("mousemove", moved, true);
        for (const event of ["statechange", "loadeddata"])
            player.removeEventListener(event, refresh);
        document.removeEventListener("visibilitychange", refresh);
        player.removeEventListener("loadstart", reset);
        player.player?.stopPreviewFill();
        shadow.adoptedStyleSheets = shadow.adoptedStyleSheets.filter(
            (sheet) => sheet !== hoverStyle,
        );
    };
}
