import { useEffect, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { LoaderCircle, Play, RefreshCw } from "lucide-react";
import type { MoviElement } from "movi-player/element/slim";
import wasmUrl from "movi-player/movi.wasm?url";
import MovTextRenderer, { isMovTextCodec } from "./MovTextRenderer";
import {
    attachVideoPresentation,
    refreshVideoActions,
    clearVideoQualityControl,
    installVideoPresentation,
    setVideoQualityControl,
    type VideoQualityControl,
} from "./videoPlayerPresentation";
import { installVideoPlayerPip } from "./videoPlayerPip";
import { attachVideoGestures } from "./videoPlayerGestures";
import { attachVideoPreviews } from "./videoPlayerPreviews";
import { formatBytes } from "../../../shared/types";
import { videoSourceMemoryBudgetMiB } from "./videoBufferMemory";
import { guardVideoPosterSeek, loadVideoPoster } from "./videoPoster";
import {
    formatVideoTime,
    readVideoPosition,
    saveVideoPosition,
} from "./videoWatchProgress";
import "./AdvancedVideo.css";
import advancedStyles from "./AdvancedVideo.css?inline";

let stylesInstalled = false;
/** Compatibility adapter for pinned 0.4.1: retain strict style CSP and local fonts. */
function installLocalStyles(constructor: typeof MoviElement) {
    if (stylesInstalled) return;
    const prototype = constructor.prototype as unknown as {
        addStyles: (shadow: ShadowRoot) => void;
    };
    const original = prototype.addStyles;
    if (
        typeof original !== "function" ||
        !("adoptedStyleSheets" in ShadowRoot.prototype)
    )
        throw new Error("Enhanced playback is unavailable in this browser.");
    prototype.addStyles = function (shadow: ShadowRoot) {
        const sheets: CSSStyleSheet[] = [];
        // The library writes its static stylesheet and optional Google-font import
        // through these two methods. Never insert either inline stylesheet in the DOM.
        const target = {
            querySelector(selector: string) {
                return selector === "style[data-movi-font]"
                    ? shadow.host
                    : shadow.querySelector(selector);
            },
            appendChild(node: Node) {
                if (!(node instanceof HTMLStyleElement))
                    throw new Error(
                        "Unexpected enhanced-player style content.",
                    );
                const sheet = new CSSStyleSheet();
                sheet.replaceSync(
                    (node.textContent || "").replace(/@import[^;]+;/g, ""),
                );
                sheets.push(sheet);
                return node;
            },
        };
        original.call(this, target as unknown as ShadowRoot);
        const focus = new CSSStyleSheet();
        focus.replaceSync(
            "button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:2px solid #b9c6ff!important;outline-offset:3px!important}" +
                advancedStyles,
        );
        shadow.adoptedStyleSheets = [
            ...shadow.adoptedStyleSheets,
            ...sheets,
            focus,
        ];
    };
    stylesInstalled = true;
}
export type VideoLoadState = {
    phase:
        "player" | "opening" | "preparing" | "ready" | "buffering" | "seeking";
    ready: boolean;
};
type VideoLoadEvent =
    | "reset"
    | "engine-loaded"
    | "loadstart"
    | "trackschange"
    | "loadedmetadata"
    | "loadeddata"
    | "canplay"
    | "ready"
    | "playing"
    | "waiting"
    | "seeking"
    | "seeked"
    | "pause"
    | "ended";
export const initialVideoLoadState: VideoLoadState = {
    phase: "player",
    ready: false,
};
/** Metadata readiness and playback intent are distinct: opening never needs autoplay. */
export function videoLoadTransition(
    state: VideoLoadState,
    event: VideoLoadEvent,
): VideoLoadState {
    switch (event) {
        case "reset":
            return initialVideoLoadState;
        case "engine-loaded":
        case "loadstart":
            return { phase: "opening", ready: false };
        case "trackschange":
            return state.ready ? state : { phase: "preparing", ready: false };
        case "loadedmetadata":
        case "loadeddata":
        case "canplay":
        case "ready":
        case "playing":
            return state.phase === "ready"
                ? state
                : { phase: "ready", ready: true };
        case "waiting":
            return state.ready
                ? { phase: "buffering", ready: true }
                : { phase: "preparing", ready: false };
        case "seeking":
            return state.ready
                ? { phase: "seeking", ready: true }
                : { phase: "preparing", ready: false };
        case "seeked":
        case "pause":
        case "ended":
            return state.ready ? { phase: "ready", ready: true } : state;
    }
}
/** Only real container chapters should expose the player's chapter timeline. */
export function videoHasChapters(
    chapters: { start: number }[],
    duration: number,
): boolean {
    return chapters.some(
        (chapter) =>
            Number.isFinite(chapter.start) &&
            chapter.start >= 0 &&
            (!Number.isFinite(duration) ||
                duration <= 0 ||
                chapter.start < duration),
    );
}
/** Advancing pictures clear a stale buffering event; a seek jump is not playback. */
export function videoIsAdvancing(
    previous: number,
    current: number,
    paused: boolean,
    seeking: boolean,
): boolean {
    return (
        !paused &&
        !seeking &&
        Number.isFinite(previous) &&
        Number.isFinite(current) &&
        current - previous > 0 &&
        current - previous < 2
    );
}
/** Pinned Movi 0.4.1 adapter: a frozen picture must also hold sound and the clock. */
export function holdVideoForBuffering(core: unknown, now: number): void {
    const player = core as {
        getState: () => string;
        getBindAV: () => boolean;
        hasPicture: boolean;
        isAudioOnly: () => boolean;
        isPastVideoEnd: () => boolean;
        isBackgrounded: boolean;
        isPiPActive: boolean;
        eofReached: boolean;
        _eofFlushRequested: boolean;
        _eofFlushSeq: number;
        _eofFlushSettledSeq: number;
        videoDecoder: { queueSize: number };
        videoRenderer: {
            getQueueSize: () => number;
            stopPresentationLoop: () => void;
        };
        audioRenderer: { suspendForBuffering: () => void };
        clock: { pause: () => void };
        stateManager: { setState: (state: string) => void };
        cancelBlackFrameWatchdog: () => void;
        wasPlayingBeforeRebuffer: boolean;
        _bufferingEntryTime: number;
        _bufferingSelfInflicted: boolean;
        _bufferingSupplyIsFine: boolean;
        _soundCarryingAlone: boolean;
    } | null;
    if (
        player?.getState() !== "playing" ||
        !player.getBindAV() ||
        !player.hasPicture ||
        player.isAudioOnly() ||
        player.isPastVideoEnd() ||
        (player.isBackgrounded && !player.isPiPActive)
    )
        return;
    // A fully drained video can leave a soundtrack tail with no next picture to await.
    if (
        player.eofReached &&
        player._eofFlushRequested &&
        player._eofFlushSettledSeq === player._eofFlushSeq &&
        player.videoDecoder.queueSize === 0 &&
        player.videoRenderer.getQueueSize() === 0
    )
        return;
    player.cancelBlackFrameWatchdog();
    player._soundCarryingAlone = false;
    player.wasPlayingBeforeRebuffer = true;
    player._bufferingEntryTime = now;
    player._bufferingSelfInflicted = false;
    player._bufferingSupplyIsFine = false;
    player.stateManager.setState("buffering");
    player.clock.pause();
    player.audioRenderer.suspendForBuffering();
    player.videoRenderer.stopPresentationLoop();
}
/** Saved positions can come from a longer rendition; never seek past its end. */
export function videoResumeTime(time: number, duration?: number): number {
    const target = Number.isFinite(time) ? Math.max(0, time) : 0;
    return duration !== undefined && Number.isFinite(duration) && duration > 0
        ? Math.min(target, Math.max(0, duration - 0.1))
        : target;
}
/** Read-only adapter for pinned Movi 0.4.1, after decoder initialization settles.
 * That version can announce ready even when its software decoder rejected a codec.
 * Missing adapter fields are inconclusive; never guess from a filename or a timer.
 */
export function unavailableVideoCodec(core: unknown): string | null {
    const player = core as {
        videoDecoder?: { configured?: boolean };
        getMediaInfo?: () => {
            tracks?: {
                type?: string;
                codec?: string;
                isAttachedPic?: boolean;
            }[];
        } | null;
    } | null;
    if (player?.videoDecoder?.configured !== false) return null;
    const video = player
        .getMediaInfo?.()
        ?.tracks?.find(
            (track) => track.type === "video" && !track.isAttachedPic,
        );
    if (!video) return null;
    return video.codec && /^[a-z0-9_.-]{1,48}$/i.test(video.codec)
        ? video.codec
        : "unknown";
}
export type VideoBufferSnapshot = {
    ranges: { start: number; end: number }[];
    received: number;
    bytesPerSecond: number;
    measured: boolean;
    decodedSeconds: number;
    deliveryWaiting: boolean;
    decodeBound: boolean;
    softwareDecoding: boolean;
};
/** Movi's source-window time ranges are estimates, not decoded readiness. */
export function videoBufferSnapshot(player: {
    duration: number;
    currentTime: number;
    buffered: TimeRanges;
    player?: {
        getSource?: () => unknown;
        pictureRunwaySeconds?: () => number;
        deliveryStarved?: () => boolean;
        isDecodeBound?: () => boolean;
        isSoftwareDecoding?: () => boolean;
    } | null;
}): VideoBufferSnapshot {
    const duration = player.duration;
    const bounded = Number.isFinite(duration) && duration > 0;
    const ranges: VideoBufferSnapshot["ranges"] = [];
    for (let index = 0; index < player.buffered.length; index++) {
        const start = Math.max(0, player.buffered.start(index));
        const rawEnd = player.buffered.end(index);
        const end = bounded ? Math.min(duration, rawEnd) : rawEnd;
        if (Number.isFinite(start) && Number.isFinite(end) && end > start)
            ranges.push({ start, end });
    }
    ranges.sort((left, right) => left.start - right.start);
    const merged: typeof ranges = [];
    for (const range of ranges) {
        const last = merged.at(-1);
        if (last && range.start <= last.end)
            last.end = Math.max(last.end, range.end);
        else merged.push({ ...range });
    }
    const source = player.player?.getSource?.() as {
        getNetworkStats?: () => { totalBytes?: number; currentSpeed?: number };
    } | null;
    const stats = source?.getNetworkStats?.();
    const positive = (value: number | undefined) =>
        value !== undefined && Number.isFinite(value) && value > 0 ? value : 0;
    return {
        ranges: merged,
        received: positive(stats?.totalBytes),
        bytesPerSecond: positive(stats?.currentSpeed),
        measured:
            Number.isFinite(stats?.totalBytes) &&
            (stats?.totalBytes ?? -1) >= 0,
        decodedSeconds: positive(player.player?.pictureRunwaySeconds?.()),
        deliveryWaiting: player.player?.deliveryStarved?.() === true,
        decodeBound: player.player?.isDecodeBound?.() === true,
        softwareDecoding: player.player?.isSoftwareDecoding?.() === true,
    };
}
const phaseCopy = {
    player: "Loading video…",
    opening: "Loading video…",
    preparing: "Loading video…",
    buffering: "Waiting for video…",
    seeking: "Loading this part…",
};
export default function AdvancedVideo({
    fileId,
    src,
    posterUrl,
    name,
    playback,
    qualityControl,
}: {
    fileId: string;
    src: string;
    posterUrl: string;
    name: string;
    playback: { current: { time: number; playing: boolean } | null };
    qualityControl?: VideoQualityControl;
}) {
    const host = useRef<HTMLDivElement>(null);
    const activePlayer = useRef<MoviElement | null>(null);
    const restorePlayerFocus = useRef(false);
    const qualityControlRef = useRef(qualityControl);
    qualityControlRef.current = qualityControl;
    const currentQualityControl = (): VideoQualityControl | undefined => {
        const control = qualityControlRef.current;
        if (!control) return;
        return {
            ...control,
            onChange: (value) => {
                restorePlayerFocus.current = true;
                const surface = host.current?.parentElement;
                // Keep the stable player surface fullscreen while replacing its engine.
                // Request synchronously from the quality button's user activation.
                if (
                    document.fullscreenElement === activePlayer.current &&
                    surface
                ) {
                    void surface
                        .requestFullscreen()
                        .catch(() => {})
                        .then(() => {
                            qualityControlRef.current?.onChange(value);
                        });
                } else control.onChange(value);
            },
        };
    };
    useEffect(() => {
        if (activePlayer.current)
            setVideoQualityControl(
                activePlayer.current,
                currentQualityControl(),
            );
    }, [qualityControl]);
    const [state, dispatch] = useReducer(
        videoLoadTransition,
        initialVideoLoadState,
    );
    const [attempt, setAttempt] = useState(0);
    const [slow, setSlow] = useState(false);
    const [waitSeconds, setWaitSeconds] = useState(0);
    const [showStatus, setShowStatus] = useState(false);
    const [playbackError, setPlaybackError] = useState("");
    const [continueTime, setContinueTime] = useState(() =>
        readVideoPosition(fileId),
    );
    const [transfer, setTransfer] = useState<VideoBufferSnapshot | null>(null);
    const waitingForVideo = useRef(true);
    waitingForVideo.current = state.phase !== "ready";
    const [visible, setVisible] = useState(!document.hidden);
    const [statusSurface, setStatusSurface] = useState<ShadowRoot | null>(null);
    useEffect(() => {
        setShowStatus(false);
        if (state.phase === "ready") return;
        // Short startup work stays quiet. Playback waits are already debounced below.
        const timer = window.setTimeout(
            () => setShowStatus(true),
            state.ready ? 0 : 500,
        );
        return () => clearTimeout(timer);
    }, [state.phase, state.ready, attempt]);
    useEffect(() => {
        const update = () => setVisible(!document.hidden);
        document.addEventListener("visibilitychange", update);
        return () => document.removeEventListener("visibilitychange", update);
    }, []);
    useEffect(() => {
        const update = () => {
            activePlayer.current?.setHostFullscreen(
                document.fullscreenElement === host.current?.parentElement,
            );
            setStatusSurface(
                document.fullscreenElement === activePlayer.current
                    ? activePlayer.current?.shadowRoot || null
                    : null,
            );
        };
        document.addEventListener("fullscreenchange", update);
        return () => document.removeEventListener("fullscreenchange", update);
    }, []);
    useEffect(() => {
        setWaitSeconds(0);
        if (state.phase === "ready") return;
        const began = performance.now();
        const timer = window.setInterval(
            () =>
                setWaitSeconds(Math.floor((performance.now() - began) / 1000)),
            1000,
        );
        return () => clearInterval(timer);
    }, [state.phase, attempt]);
    useEffect(() => {
        setSlow(false);
        if (state.phase === "ready") return;
        // This only offers recovery. A long wait is never proof that playback failed.
        const timer = window.setTimeout(() => setSlow(true), 8000);
        return () => clearTimeout(timer);
    }, [state.phase, attempt]);
    useEffect(() => {
        let cancelled = false;
        let failed = false;
        let codecRejected = false;
        let usable = false;
        let player: MoviElement | null = null;
        let subtitleRenderer: MovTextRenderer | null = null;
        let progressTimer: number | undefined;
        let frameCallback: number | undefined;
        let frameSource: MoviElement | null = null;
        let lastPlayback: { time: number; playing: boolean } | null = null;
        let watchStarted = false;
        let representativePoster = false;
        let posterSource: string | null = null;
        const posterController = new AbortController();
        // Closing the preview can move an ended engine back to paused.
        let finished = false;
        let lastSavedAt = -Infinity;
        let previousTime = Number.NaN;
        let latestProgressAt = 0;
        let hasPresentedFrame = false;
        let detachPresentation: (() => void) | undefined;
        let detachPip: (() => void) | undefined;
        let detachGestures: (() => void) | undefined;
        let detachPreviews: (() => void) | undefined;
        let detachPosterSeek: (() => void) | undefined;
        const expectsPicture = () => {
            const core = player?.player;
            if (!core?.getMediaInfo()) return true;
            return core.getVideoTracks().some((track) => !track.isAttachedPic);
        };
        const updateChapters = () => {
            if (!player || cancelled) return;
            const hasChapters = videoHasChapters(
                player.player?.getChapters() || [],
                player.duration,
            );
            // Only real chapters get a strip; their images are filled on demand.
            const controls = hasChapters ? "nospinner" : "notimeline nospinner";
            if (player.getAttribute("controlslist") !== controls)
                player.setAttribute("controlslist", controls);
            refreshVideoActions(player);
        };
        const updateSubtitleRenderer = () => {
            if (!player || cancelled) return;
            const track = player.player?.trackManager.getActiveSubtitleTrack();
            if (isMovTextCodec(track?.codec)) {
                if (!subtitleRenderer) {
                    subtitleRenderer = new MovTextRenderer();
                    player.setSubtitleRenderer(subtitleRenderer);
                }
            } else if (subtitleRenderer) {
                player.setSubtitleRenderer(null);
                subtitleRenderer.destroy();
                subtitleRenderer = null;
            }
        };
        let resume: {
            source: { time: number; playing: boolean };
            target: number;
            positionApplied: boolean;
            playRequested: boolean;
        } | null = null;
        let waitingTimer: number | undefined;
        let pendingSeek = false;
        let pendingWait: "waiting" | "seeking" = "waiting";
        const listeners: [string, EventListener][] = [];
        dispatch("reset");
        setPlaybackError("");
        setTransfer(null);
        let lastTransferAt = 0;
        let waitReceivedFrom = 0;
        const clearWaiting = () => {
            clearTimeout(waitingTimer);
            waitingTimer = undefined;
        };
        const recover = () => {
            if (!failed || codecRejected) return;
            failed = false;
            setPlaybackError("");
        };
        const fail = (event?: Event) => {
            if (cancelled) return;
            // Movi retries dropped connections itself. A reconnect is a wait, not a
            // permanent error, and playback can recover without the user's help.
            if (
                event?.type === "errordisplay" &&
                (event as CustomEvent<{ title?: string }>).detail?.title ===
                    "Reconnecting…"
            ) {
                recover();
                waiting("waiting");
                return;
            }
            if (!cancelled && !failed) {
                failed = true;
                clearWaiting();
                setPlaybackError(
                    "The video could not play. Try again, or download it to watch on your device.",
                );
            }
        };
        const updateStartPosition = () => {
            if (!resume || !player || resume.positionApplied) return;
            resume.target = videoResumeTime(resume.target, player.duration);
            // Movi emits ready/loadEnd before its initializer performs its startup seek.
            // Updating startat here clamps the owned seek without racing a second seek.
            player.setAttribute("startat", String(resume.target));
        };
        const observeResumePosition = () => {
            if (!usable || !resume || !player || resume.positionApplied) return;
            if (Math.abs(player.currentTime - resume.target) > 0.05) return;
            resume.positionApplied = true;
            if (playback.current === resume.source) playback.current = null;
        };
        const savePosition = (force = false) => {
            // Startup seeks must not overwrite a Continue offer that was never used.
            if (
                !watchStarted ||
                !lastPlayback ||
                (resume && !resume.positionApplied)
            )
                return;
            const now = performance.now();
            if (!force && now - lastSavedAt < 5000) return;
            saveVideoPosition(
                fileId,
                finished || player?.ended ? 0 : lastPlayback.time,
            );
            lastSavedAt = now;
        };
        const startWatching = () => {
            watchStarted = true;
            finished = false;
            setContinueTime(null);
        };
        const rememberPlayback = () => {
            if (
                ((representativePoster || posterSource) && !watchStarted) ||
                !usable ||
                !player?.player ||
                player.player.getState() === "error" ||
                !Number.isFinite(player.currentTime) ||
                player.currentTime < 0
            )
                return;
            lastPlayback = {
                time: videoResumeTime(player.currentTime, player.duration),
                playing: !player.paused,
            };
            if (!player.seeking && !player.paused) savePosition();
        };
        const flushPosition = () => {
            rememberPlayback();
            savePosition(true);
        };
        const saveWhenHidden = () => {
            if (document.hidden) flushPosition();
        };
        document.addEventListener("visibilitychange", saveWhenHidden);
        window.addEventListener("pagehide", flushPosition);
        const updateBuffer = () => {
            if (!player || cancelled || failed) return;
            const sample = videoBufferSnapshot(player);
            // Source counters include repeat/seek reads. They describe delivery to this
            // player, never whole-file completion or how many pictures can be decoded.
            if (
                waitingForVideo.current &&
                performance.now() - lastTransferAt >= 500
            ) {
                lastTransferAt = performance.now();
                setTransfer({
                    ...sample,
                    received: Math.max(0, sample.received - waitReceivedFrom),
                });
            }
            // Pinned Movi 0.4.1 can leave this documented CSS part at zero while
            // paused, even with a ready buffer. Paint its own public TimeRanges,
            // including the seek origin, instead of converting server byte offsets.
            const bar = player.shadowRoot?.querySelector<HTMLElement>(
                '[part~="progress-buffer"]',
            );
            const range = sample.ranges[0]; // This engine exposes one contiguous source window.
            if (
                bar &&
                Number.isFinite(player.duration) &&
                player.duration > 0
            ) {
                bar.title = "Loaded video (approximate)";
                bar.style.left =
                    (range ? (range.start / player.duration) * 100 : 0) + "%";
                bar.style.width =
                    (range
                        ? ((range.end - range.start) / player.duration) * 100
                        : 0) + "%";
            }
        };
        const ready = (
            event:
                | "loadedmetadata"
                | "loadeddata"
                | "canplay"
                | "ready"
                | "playing",
        ) => {
            if (cancelled || codecRejected) return;
            if (
                failed &&
                (event === "loadedmetadata" || event === "loadeddata")
            )
                return;
            if (event === "canplay" || event === "ready") {
                const codec = unavailableVideoCodec(player?.player);
                if (codec) {
                    failed = true;
                    codecRejected = true;
                    clearWaiting();
                    setPlaybackError(
                        "This video’s codec" +
                            (codec === "unknown" ? "" : " (" + codec + ")") +
                            " is not supported by the player on this device. Download the original to open it in a video app.",
                    );
                    return;
                }
            }
            recover();
            usable = true;
            updateBuffer();
            updateChapters();
            updateSubtitleRenderer();
            const engineState = player?.player?.getState();
            if (
                event !== "playing" &&
                (engineState === "buffering" || engineState === "seeking")
            ) {
                waiting(engineState === "seeking" ? "seeking" : "waiting");
                return;
            }
            if (event === "playing" && expectsPicture() && !hasPresentedFrame) {
                // The engine starts its clock before the first decoded picture arrives.
                // Preserve loading feedback until successive real frames are presented.
                waiting("waiting");
                return;
            }
            clearWaiting();
            pendingSeek = false;
            dispatch(event);

            updateStartPosition();
            if (resume && player && !resume.playRequested) {
                resume.playRequested = true;
                // During initialization play() queues intent until Movi's startat seek
                // completes. Fresh previews and previously paused videos stay paused.
                if (resume.source.playing) void player.play().catch(() => {});
            }
            observeResumePosition();
            rememberPlayback();
        };
        const waiting = (kind: "waiting" | "seeking") => {
            if (cancelled || failed) return;
            if (!usable) {
                dispatch("trackschange");
                return;
            }
            if (kind === "waiting" && player?.paused) return;
            if (kind === "seeking" && player) {
                if (!pendingSeek)
                    waitReceivedFrom = videoBufferSnapshot(player).received;
                pendingSeek = true;
                previousTime = Number.NaN;
                latestProgressAt = 0;
                hasPresentedFrame = false;
            }
            pendingWait = pendingSeek ? "seeking" : kind;
            if (waitingTimer !== undefined) return;
            waitingTimer = window.setTimeout(() => {
                waitingTimer = undefined;
                if (cancelled || failed || !player) return;
                if (
                    pendingWait === "waiting" &&
                    (player.paused ||
                        performance.now() - latestProgressAt < 500)
                )
                    return;
                if (
                    pendingWait === "waiting" &&
                    latestProgressAt > 0 &&
                    performance.now() - latestProgressAt > 1250
                )
                    holdVideoForBuffering(player.player, performance.now());
                hasPresentedFrame = false;
                dispatch(pendingWait);
            }, 700);
        };
        const settle = (event: "pause" | "seeked" | "ended") => {
            if (cancelled || failed) return;
            if (event === "ended") finished = true;
            clearWaiting();
            if (event !== "seeked") pendingSeek = false;
            dispatch(event);
            rememberPlayback();
            if (!player?.seeking) savePosition(true);
        };
        void Promise.all([
            import("movi-player/element/slim"),
            playback.current
                ? Promise.resolve(null)
                : loadVideoPoster(posterUrl, posterController.signal),
        ])
            .then(([{ MoviElement }, poster]) => {
                if (cancelled || !host.current) return;
                installLocalStyles(MoviElement);
                installVideoPresentation(MoviElement);
                player = document.createElement("movi-player");
                activePlayer.current = player;
                setVideoQualityControl(player, currentQualityControl());
                detachPresentation = attachVideoPresentation(player);
                detachPip = installVideoPlayerPip(player);
                detachGestures = attachVideoGestures(player);
                detachPreviews = attachVideoPreviews(player);
                player.setAttribute("wasmurl", wasmUrl);
                player.setAttribute("engine", "wasm");
                player.setAttribute("sw", "auto");
                // This ceiling is separate from decoded pictures. The source allocates
                // gradually toward its bitrate-aware time goal, rather than reserving
                // the entire device budget for every video.
                player.setAttribute(
                    "buffersize",
                    String(
                        videoSourceMemoryBudgetMiB(
                            (navigator as Navigator & { deviceMemory?: number })
                                .deviceMemory,
                        ),
                    ),
                );
                player.setAttribute("persist", "");
                player.setAttribute("controls", "");
                player.setAttribute("subtitlepicker", "");
                player.setAttribute("playsinline", "");
                player.setAttribute("preload", "metadata");
                player.setAttribute("theme", "dark");
                player.setAttribute("themecolor", "#647dff");
                player.setAttribute("fastseek", "keys gestures");
                // Keyframes reuse the player's caches without precise frame walks.
                player.setAttribute("thumb", "");
                player.setAttribute("controlslist", "notimeline nospinner");
                player.setAttribute(
                    "aria-label",
                    "Enhanced video player for " + name,
                );
                const saved = playback.current;
                if (saved) {
                    resume = {
                        source: saved,
                        target: videoResumeTime(saved.time),
                        positionApplied: false,
                        playRequested: false,
                    };
                    // Establish the library's startup position before connection or src.
                    player.setAttribute("startat", String(resume.target));
                } else if (poster) {
                    posterSource = URL.createObjectURL(poster);
                    player.setAttribute("poster", posterSource);
                    player.setAttribute("posterfit", "contain");
                } else {
                    representativePoster = true;
                    player.setAttribute("postertime", "50%");
                }
                const listen = (type: string, callback: EventListener) => {
                    player!.addEventListener(type, callback);
                    listeners.push([type, callback]);
                };
                listen("loadstart", () => {
                    codecRejected = false;
                    recover();
                    usable = false;
                    hasPresentedFrame = false;
                    clearWaiting();
                    setTransfer(null);
                    dispatch("loadstart");
                });
                listen("trackschange", () => {
                    updateChapters();
                    dispatch("trackschange");
                });
                // TrackManager emits this synchronously before choosing the decoder.
                // Install the public extension in time to receive raw MP4 text packets.
                listen("subtitletrackchange", updateSubtitleRenderer);
                listen("loadedmetadata", () => {
                    if (representativePoster && !detachPosterSeek)
                        detachPosterSeek = guardVideoPosterSeek(player!);
                    ready("loadedmetadata");
                });
                listen("loadeddata", () => ready("loadeddata"));
                listen("canplay", () => ready("canplay"));
                listen("playing", () => ready("playing"));
                listen("play", () => {
                    if (representativePoster) player?.removeAttribute("postertime");
                    startWatching();
                    // A playing state is an intent; watch actual presented frames below.
                    latestProgressAt = performance.now();
                });
                listen("waiting", () => waiting("waiting"));
                listen("stalled", () => waiting("waiting"));
                listen("seeking", () => waiting("seeking"));
                listen("seeked", () => {
                    observeResumePosition();
                    if (usable && player && player.currentTime > 0)
                        startWatching();
                    rememberPlayback();
                    settle("seeked");
                    // A new position can be available before its audio/video refill is ready.
                    // Keep the actual engine wait visible instead of treating seeked as play.
                    if (player?.player?.getState() === "buffering")
                        waiting("waiting");
                    else pendingSeek = false;
                });
                const observeProgress = () => {
                    if (!player || cancelled) return;
                    observeResumePosition();
                    rememberPlayback();
                    updateBuffer();
                    if (usable && !expectsPicture()) {
                        if (
                            videoIsAdvancing(
                                previousTime,
                                player.currentTime,
                                player.paused,
                                player.seeking,
                            )
                        ) {
                            recover();
                            pendingSeek = false;
                            latestProgressAt = performance.now();
                            clearWaiting();
                            dispatch("playing");
                        }
                        previousTime = player.currentTime;
                    }
                    if (
                        usable &&
                        expectsPicture() &&
                        !player.paused &&
                        !player.seeking &&
                        player.currentTime < player.duration - 0.1 &&
                        latestProgressAt > 0 &&
                        performance.now() - latestProgressAt > 1250
                    )
                        waiting("waiting");
                };
                listen("timeupdate", observeProgress);
                listen("progress", observeProgress);
                listen("durationchange", updateStartPosition);
                listen("pause", () => settle("pause"));
                listen("ended", () => settle("ended"));
                listen("statechange", (event) => {
                    const phase = (event as CustomEvent<string>).detail;
                    if (
                        phase === "ready" ||
                        (phase === "paused" && player!.duration > 0)
                    )
                        ready("ready");
                    else if (phase === "playing") ready("playing");
                    else if (phase === "buffering") waiting("waiting");
                    else if (phase === "seeking") waiting("seeking");
                    else if (phase === "paused") settle("pause");
                });
                listen("error", fail);
                listen("errordisplay", fail);
                dispatch("engine-loaded");
                host.current.append(player);
                player.setHostFullscreen(
                    document.fullscreenElement === host.current.parentElement,
                );
                if (restorePlayerFocus.current) {
                    restorePlayerFocus.current = false;
                    player.focus({ preventScroll: true });
                }
                player.src = new URL(src, window.location.origin).href;
                const presentedFrame = (_now: number, metadata: unknown) => {
                    if (!player || cancelled) return;
                    const mediaTime = (
                        metadata as { mediaTime?: number } | null
                    )?.mediaTime;
                    // A clock jump or prefetched source range is not proof of moving pictures.
                    // Reset at every seek and require successive presented frames instead.
                    if (
                        !codecRejected &&
                        videoIsAdvancing(
                            previousTime,
                            mediaTime ?? Number.NaN,
                            player.paused,
                            player.seeking,
                        )
                    ) {
                        recover();
                        hasPresentedFrame = true;
                        pendingSeek = false;
                        latestProgressAt = performance.now();
                        clearWaiting();
                        dispatch("playing");
                    }
                    previousTime = mediaTime ?? Number.NaN;
                    frameCallback =
                        frameSource?.requestVideoFrameCallback(presentedFrame);
                };
                frameSource = player;
                frameCallback =
                    frameSource.requestVideoFrameCallback(presentedFrame);
                progressTimer = window.setInterval(observeProgress, 250);
            })
            .catch((event) => {
                posterController.abort();
                fail(event);
            });
        return () => {
            cancelled = true;
            posterController.abort();
            clearWaiting();
            clearInterval(progressTimer);
            document.removeEventListener("visibilitychange", saveWhenHidden);
            window.removeEventListener("pagehide", flushPosition);
            flushPosition();
            detachPresentation?.();
            detachPip?.();
            detachGestures?.();
            detachPreviews?.();
            detachPosterSeek?.();
            if (player) {
                restorePlayerFocus.current ||=
                    document.activeElement === player;
                clearVideoQualityControl(player);
                if (frameCallback !== undefined)
                    frameSource?.cancelVideoFrameCallback(frameCallback);
                if (
                    finished ||
                    ((representativePoster || posterSource) && !watchStarted)
                )
                    playback.current = null;
                else if (
                    lastPlayback &&
                    (!player.player || player.player.getState() === "error")
                )
                    playback.current = lastPlayback;
                else if (resume && !resume.positionApplied)
                    playback.current = {
                        time: resume.target,
                        playing: usable
                            ? !player.paused
                            : resume.source.playing,
                    };
                else if (usable && Number.isFinite(player.currentTime))
                    playback.current = {
                        time: videoResumeTime(
                            player.currentTime,
                            player.duration,
                        ),
                        playing: !player.paused,
                    };
                for (const [type, callback] of listeners)
                    player.removeEventListener(type, callback);
                if (subtitleRenderer) {
                    player.setSubtitleRenderer(null);
                    subtitleRenderer.destroy();
                    subtitleRenderer = null;
                }
                player.remove(); // disconnectedCallback aborts requests and destroys its owned engine.
                if (activePlayer.current === player)
                    activePlayer.current = null;
            }
            if (posterSource) URL.revokeObjectURL(posterSource);
        };
    }, [fileId, src, posterUrl, name, playback, attempt]);
    const copy = state.phase !== "ready" ? phaseCopy[state.phase] : null;
    const loadingStatus =
        playbackError || (copy && showStatus) ? (
            <div
                className={
                    "advanced-state " +
                    (playbackError
                        ? "advanced-state-error"
                        : "advanced-state-buffering")
                }
                data-phase={state.phase}
                data-hidden={!visible}
                data-error={Boolean(playbackError)}
            >
                <div className="advanced-state-content">
                    <div
                        role={playbackError ? "alert" : "status"}
                        aria-live="polite"
                        aria-atomic="true"
                    >
                        {playbackError ? (
                            <>
                                <h3>Can’t play this video</h3>
                                <p>{playbackError}</p>
                            </>
                        ) : (
                            <div className="advanced-wait-label">
                                <span
                                    role="progressbar"
                                    aria-label="Loading video"
                                >
                                    <LoaderCircle
                                        size={18}
                                        aria-hidden="true"
                                    />
                                </span>
                                <span>{copy}</span>
                                {state.ready && waitSeconds > 0 && (
                                    <span className="advanced-wait-elapsed">
                                        {waitSeconds}s
                                    </span>
                                )}
                            </div>
                        )}
                    </div>
                    {!playbackError && transfer?.measured && (
                        <div className="advanced-transfer" aria-live="off">
                            <div className="advanced-transfer-network">
                                <span>
                                    {formatBytes(transfer.received)} received
                                    {state.phase === "seeking"
                                        ? " for this seek"
                                        : ""}
                                </span>
                                {transfer.bytesPerSecond > 0 && (
                                    <span>
                                        {formatBytes(transfer.bytesPerSecond)}/s
                                    </span>
                                )}
                            </div>
                        </div>
                    )}
                    {(slow || playbackError) && (
                        <div className="advanced-state-actions">
                            <button
                                type="button"
                                onClick={() => setAttempt((value) => value + 1)}
                            >
                                <RefreshCw size={16} aria-hidden="true" /> Try
                                again
                            </button>
                        </div>
                    )}
                </div>
            </div>
        ) : null;
    const continueButton =
        continueTime !== null &&
        state.ready &&
        !playbackError &&
        continueTime < (activePlayer.current?.duration ?? 0) ? (
            <div className="advanced-state-actions advanced-continue">
                <button
                    type="button"
                    onClick={() => {
                        const player = activePlayer.current;
                        const core = player?.player;
                        if (!player || !core) return;
                        setContinueTime(null);
                        player.focus({ preventScroll: true });
                        // Fresh paused loads need a settled seek before first play.
                        const onSeeked = () => {
                            if (activePlayer.current === player)
                                void player.play().catch(() => {});
                        };
                        core.once("seeked", onSeeked);
                        void core
                            .seek(
                                videoResumeTime(continueTime, player.duration),
                            )
                            .catch(() => {
                                core.off("seeked", onSeeked);
                                if (activePlayer.current === player)
                                    setContinueTime(continueTime);
                            });
                    }}
                >
                    <Play size={16} aria-hidden="true" /> Continue from{" "}
                    {formatVideoTime(continueTime)}
                </button>
            </div>
        ) : null;
    const overlays = (
        <>
            {loadingStatus}
            {continueButton}
        </>
    );
    return (
        <div className="advanced-video" data-phase={state.phase}>
            <div
                ref={host}
                className="advanced-player-host"
                aria-busy={state.phase !== "ready" && !playbackError}
            />
            {statusSurface ? createPortal(overlays, statusSurface) : overlays}
        </div>
    );
}
