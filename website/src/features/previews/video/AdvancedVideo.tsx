import {
    useEffect,
    useLayoutEffect,
    useReducer,
    useRef,
    useState,
} from "react";
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
    createVideoProgressWriter,
    loadServerVideoPosition,
    savedVideoResumeTime,
    type VideoProgressMode,
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
    player: "Initializing video engine…",
    opening: "Opening video…",
    preparing: "Preparing media tracks…",
    buffering: "Buffering video…",
    seeking: "Loading this part…",
};
/** Bytes alone do not prove that a decoder can make playback usable. */
export function videoWaitAllowsRetry(idleMs: number, waitMs: number) {
    return idleMs >= 15000 || waitMs >= 60000;
}
export default function AdvancedVideo({
    fileId,
    progressMode,
    src,
    posterUrl,
    name,
    playback,
    qualityControl,
}: {
    fileId: string;
    progressMode: VideoProgressMode | null;
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
    const [seekAttempt, setSeekAttempt] = useState(0);
    const [showStatus, setShowStatus] = useState(false);
    const [playbackError, setPlaybackError] = useState("");
    const [continueTime, setContinueTime] = useState<number | null>(null);
    const [progressNotice, setProgressNotice] = useState("");
    const [progressAttempt, setProgressAttempt] = useState(0);
    const [progressDecision, setProgressDecision] = useState<
        "pending" | "ready" | "choice" | "failed"
    >("pending");
    const decision = useRef(progressDecision);
    const decide = (value: typeof progressDecision) => {
        decision.current = value;
        setProgressDecision(value);
    };
    const manualPosition = useRef(false);
    const knownPosition = useRef<number | null>(null);
    const scopeNeedsChoice = useRef(false);
    const previousProgressMode = useRef(progressMode);
    const resumeAppliedPlayer = useRef<MoviElement | null>(null);
    const resumedPosition = useRef<number | null>(null);
    const [resumedTime, setResumedTime] = useState<number | null>(null);
    const [resumeNoticeFocused, setResumeNoticeFocused] = useState(false);
    const seekSavedPosition = useRef<
        ((time: number, play: boolean) => Promise<boolean>) | null
    >(null);
    const flushProgress = useRef<(() => void) | null>(null);
    const cancelSavedSeek = useRef<(() => void) | null>(null);
    const progressGeneration = useRef(0);
    const progressWriter = useRef<ReturnType<
        typeof createVideoProgressWriter
    > | null>(null);
    const progressAccess = useRef(progressMode);
    progressAccess.current = progressMode;
    const progressActive = useRef(false);
    const hasStartedWatching = useRef(false);
    const playbackHasStarted = useRef(false);
    const closingPosition = useRef<
        (() => { time: number; duration: number } | null) | null
    >(null);
    const newProgressWriter = () =>
        createVideoProgressWriter(fileId, setProgressNotice);
    const showResumedNotice = () => {
        if (resumedPosition.current === null || decision.current !== "ready")
            return;
        setResumedTime(resumedPosition.current);
        resumedPosition.current = null;
    };
    const acceptCurrentProgress = (manual = false, time?: number) => {
        if (manual) manualPosition.current = true;
        if (manual) knownPosition.current = null;
        scopeNeedsChoice.current = false;
        decide("ready");
        setContinueTime(null);
        setProgressNotice("");
        if (
            progressAccess.current === "server" &&
            (playbackHasStarted.current || time !== undefined)
        ) {
            if (progressWriter.current?.closed)
                progressWriter.current = newProgressWriter();
            progressWriter.current?.start();
            if (time !== undefined && activePlayer.current)
                progressWriter.current?.save(
                    time,
                    activePlayer.current.duration,
                );
        }
        if (time === undefined) flushProgress.current?.();
        else if (
            progressAccess.current === "share" ||
            progressAccess.current === "demo"
        )
            saveVideoPosition(
                fileId,
                time,
                progressAccess.current,
                activePlayer.current?.duration,
            );
    };
    const finalizeProgress = () => {
        const writer = progressWriter.current;
        if (!writer) return;
        const saved = closingPosition.current?.();
        if (saved) writer.save(saved.time, saved.duration);
        writer.close();
        progressWriter.current = null;
    };
    useEffect(() => {
        hasStartedWatching.current = false;
        playbackHasStarted.current = false;
        manualPosition.current = false;
        scopeNeedsChoice.current = false;
        previousProgressMode.current = progressMode;
        knownPosition.current = null;
        resumeAppliedPlayer.current = null;
        resumedPosition.current = null;
        setResumedTime(null);
    }, [fileId]);
    useEffect(() => {
        const controller = new AbortController();
        progressGeneration.current++;
        progressActive.current = true;
        const changedScope = Boolean(
            progressMode &&
            progressMode !== "unavailable" &&
            previousProgressMode.current &&
            previousProgressMode.current !== "unavailable" &&
            previousProgressMode.current !== progressMode,
        );
        if (progressMode && progressMode !== "unavailable")
            previousProgressMode.current = progressMode;
        if (changedScope) manualPosition.current = false;
        scopeNeedsChoice.current ||= changedScope;
        knownPosition.current = null;
        decide("pending");
        resumeAppliedPlayer.current = null;
        setContinueTime(null);
        progressWriter.current =
            progressMode === "server" ? newProgressWriter() : null;
        if (progressMode === "server") {
            if (manualPosition.current) {
                acceptCurrentProgress();
            } else {
                setProgressNotice("Checking saved video position…");
                void loadServerVideoPosition(fileId, controller.signal)
                    .then((saved) => {
                        if (controller.signal.aborted || manualPosition.current)
                            return;
                        knownPosition.current = saved?.position_seconds ?? null;
                        if (saved && saved.position_seconds > 0)
                            setContinueTime(saved.position_seconds);
                        else if (scopeNeedsChoice.current) decide("choice");
                        else acceptCurrentProgress();
                        setProgressNotice("");
                    })
                    .catch(() => {
                        if (
                            !controller.signal.aborted &&
                            !manualPosition.current
                        ) {
                            decide("failed");
                            setProgressNotice(
                                "Saved video progress is unavailable. Playback still works; your saved position is unchanged.",
                            );
                        }
                    });
            }
        } else if (progressMode === "share" || progressMode === "demo") {
            const saved = readVideoPosition(fileId, progressMode);
            knownPosition.current = saved;
            if (!manualPosition.current && saved !== null)
                setContinueTime(saved);
            else if (scopeNeedsChoice.current) decide("choice");
            else acceptCurrentProgress();
            setProgressNotice("");
        } else
            setProgressNotice(
                progressMode === null
                    ? "Checking video resume access…"
                    : "Video resume access could not be checked. Playback still works; reopen the video to check again.",
            );
        return () => {
            progressActive.current = false;
            controller.abort();
            cancelSavedSeek.current?.();
            progressGeneration.current++;
            if (progressMode === "share" || progressMode === "demo") {
                const saved = closingPosition.current?.();
                if (saved)
                    saveVideoPosition(
                        fileId,
                        saved.time,
                        progressMode,
                        saved.duration,
                    );
            }
            finalizeProgress();
            decision.current = "pending";
        };
    }, [fileId, progressMode, progressAttempt]);
    useEffect(() => {
        if (resumedTime === null || resumeNoticeFocused) return;
        const timer = setTimeout(() => setResumedTime(null), 10000);
        return () => clearTimeout(timer);
    }, [resumedTime, resumeNoticeFocused]);
    useEffect(() => {
        const player = activePlayer.current;
        const saved =
            continueTime ??
            (!hasStartedWatching.current ? knownPosition.current : null);
        if (
            saved === null ||
            !state.ready ||
            !player ||
            !(player.duration > 0) ||
            manualPosition.current ||
            resumeAppliedPlayer.current === player
        )
            return;
        const target = savedVideoResumeTime(saved, player.duration);
        if (target === 0) {
            if (scopeNeedsChoice.current) decide("choice");
            else acceptCurrentProgress();
        } else if (hasStartedWatching.current) {
            decide("choice");
        } else {
            resumeAppliedPlayer.current = player;
            const generation = progressGeneration.current;
            void seekSavedPosition.current?.(target, false).then((applied) => {
                if (
                    activePlayer.current !== player ||
                    progressGeneration.current !== generation ||
                    !applied ||
                    manualPosition.current
                )
                    return;
                resumedPosition.current = target;
                acceptCurrentProgress();
            });
        }
    }, [continueTime, state.ready, attempt]);
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
        setSlow(false);
        if (state.phase === "ready") return;
        const began = performance.now();
        let activityAt = began;
        let received = activePlayer.current
            ? videoBufferSnapshot(activePlayer.current).received
            : 0;
        let time = activePlayer.current?.currentTime ?? 0;
        let decoded = 0;
        const timer = window.setInterval(() => {
            const now = performance.now();
            const player = activePlayer.current;
            if (player) {
                const sample = videoBufferSnapshot(player);
                if (sample.received > received) activityAt = now;
                if (
                    sample.decodedSeconds > decoded ||
                    videoIsAdvancing(
                        time,
                        player.currentTime,
                        player.paused,
                        player.seeking,
                    )
                ) {
                    activityAt = now;
                }
                received = sample.received;
                decoded = sample.decodedSeconds;
                time = player.currentTime;
            }
            setWaitSeconds(Math.floor((now - began) / 1000));
            setSlow(videoWaitAllowsRetry(now - activityAt, now - began));
        }, 1000);
        return () => clearInterval(timer);
    }, [state.phase, attempt, fileId, src, seekAttempt]);
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
        let lastPlayback: {
            time: number;
            playing: boolean;
            duration: number;
        } | null = null;
        let watchStarted = false;
        let progressSeek = false;
        let progressSeekTarget: number | null = null;
        let userSeek = false;
        let cancelProgressSeek: (() => void) | undefined;
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
                decision.current !== "ready" ||
                progressSeek ||
                (resume && !resume.positionApplied)
            )
                return;
            const now = performance.now();
            if (!force && now - lastSavedAt < 5000) return;
            const time = finished || player?.ended ? 0 : lastPlayback.time;
            const access = progressAccess.current;
            if (access === "server")
                progressWriter.current?.save(time, lastPlayback.duration);
            else if (access === "share" || access === "demo")
                saveVideoPosition(fileId, time, access, lastPlayback.duration);
            lastSavedAt = now;
        };
        const startWatching = (played = false) => {
            if (!progressActive.current) return;
            watchStarted = true;
            const firstWatch = !hasStartedWatching.current;
            hasStartedWatching.current = true;
            if (played) playbackHasStarted.current = true;
            if (
                played &&
                manualPosition.current &&
                decision.current === "pending" &&
                !progressSeek &&
                !player?.seeking
            )
                acceptCurrentProgress(true);
            if (!played) {
                resumedPosition.current = null;
                setResumedTime(null);
                acceptCurrentProgress(true);
            }
            if (
                firstWatch &&
                decision.current === "ready" &&
                progressAccess.current !== null &&
                progressAccess.current !== "unavailable"
            )
                setProgressNotice("");
            if (
                progressAccess.current === "server" &&
                decision.current === "ready" &&
                playbackHasStarted.current
            ) {
                if (progressWriter.current?.closed)
                    progressWriter.current = newProgressWriter();
                progressWriter.current?.start();
            }
            finished = false;
            if (decision.current === "ready") setContinueTime(null);
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
                duration: player.duration,
            };
            if (!player.seeking && !player.paused) savePosition();
        };
        const flushPosition = () => {
            rememberPlayback();
            savePosition(true);
        };
        flushProgress.current = flushPosition;
        const seekSaved = async (time: number, play: boolean) => {
            const target = player;
            const core = target?.player;
            if (
                !target ||
                !core ||
                cancelled ||
                posterController.signal.aborted
            )
                return false;
            cancelProgressSeek?.();
            progressSeek = true;
            progressSeekTarget = time;
            if (!watchStarted) target.setAttribute("startat", String(time));
            target.removeAttribute("postertime");
            const applied = await new Promise<boolean>((resolve) => {
                let done = false;
                const finish = (success: boolean) => {
                    if (done) return;
                    done = true;
                    core.off("seeked", complete);
                    core.off("error", failSeek);
                    posterController.signal.removeEventListener("abort", abort);
                    if (cancelProgressSeek === abort) {
                        cancelProgressSeek = undefined;
                        progressSeek = false;
                        progressSeekTarget = null;
                    }
                    if (cancelSavedSeek.current === abort)
                        cancelSavedSeek.current = null;
                    resolve(success);
                };
                const complete = () => {
                    if (Math.abs(target.currentTime - time) < 0.05)
                        finish(true);
                };
                const failSeek = () => {
                    if (done) return;
                    if (!cancelled) {
                        manualPosition.current = false;
                        resumeAppliedPlayer.current = null;
                        decide("failed");
                        setProgressNotice(
                            "The saved position could not be reached. Seek to choose a position or try again.",
                        );
                    }
                    finish(false);
                };
                const abort = () => finish(false);
                cancelProgressSeek = abort;
                cancelSavedSeek.current = abort;
                core.on("seeked", complete);
                core.on("error", failSeek);
                posterController.signal.addEventListener("abort", abort, {
                    once: true,
                });
                void core
                    .seek(videoResumeTime(time, target.duration))
                    .catch(failSeek);
            });
            if (!applied || cancelled || activePlayer.current !== target)
                return false;
            resumedPosition.current = time > 0 ? time : null;
            if (time === 0) lastSavedAt = performance.now();
            if (play) await target.play().catch(() => {});
            return true;
        };
        seekSavedPosition.current = seekSaved;
        const snapshotClosingPosition = () => {
            if (
                !watchStarted ||
                decision.current !== "ready" ||
                progressSeek ||
                (resume && !resume.positionApplied)
            )
                return null;
            if (
                usable &&
                player?.isConnected &&
                player.player &&
                player.player.getState() !== "error" &&
                Number.isFinite(player.currentTime) &&
                Number.isFinite(player.duration) &&
                player.duration > 0
            ) {
                return {
                    time:
                        finished || player.ended
                            ? 0
                            : videoResumeTime(
                                  player.currentTime,
                                  player.duration,
                              ),
                    duration: player.duration,
                };
            }
            return lastPlayback
                ? {
                      time: finished ? 0 : lastPlayback.time,
                      duration: lastPlayback.duration,
                  }
                : null;
        };
        closingPosition.current = snapshotClosingPosition;
        const closeProgress = () => {
            flushPosition();
            progressWriter.current?.close();
            playbackHasStarted.current = false;
        };
        const saveWhenHidden = () => {
            if (document.hidden) flushPosition();
        };
        document.addEventListener("visibilitychange", saveWhenHidden);
        window.addEventListener("pagehide", closeProgress);
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
                    if (representativePoster)
                        player?.removeAttribute("postertime");
                    startWatching(true);
                    // A playing state is an intent; watch actual presented frames below.
                    latestProgressAt = performance.now();
                });
                listen("waiting", () => waiting("waiting"));
                listen("stalled", () => waiting("waiting"));
                listen("seeking", (event) => {
                    const target = (event as CustomEvent<unknown>).detail;
                    const valid =
                        typeof target === "number" && Number.isFinite(target);
                    const owned =
                        valid &&
                        ((progressSeekTarget !== null &&
                            Math.abs(target - progressSeekTarget) < 0.05) ||
                            (resume &&
                                !resume.positionApplied &&
                                Math.abs(target - resume.target) < 0.05) ||
                            (!manualPosition.current &&
                                resumeAppliedPlayer.current === player &&
                                knownPosition.current !== null &&
                                Math.abs(
                                    target -
                                        savedVideoResumeTime(
                                            knownPosition.current,
                                            player!.duration,
                                        ),
                                ) < 0.05));
                    if (valid && (userSeek || !owned)) {
                        cancelProgressSeek?.();
                        manualPosition.current = true;
                        knownPosition.current = null;
                        resumedPosition.current = null;
                        setResumedTime(null);
                        player?.setAttribute("startat", String(target));
                        if (resume && !resume.positionApplied) {
                            resume.positionApplied = true;
                            resume.playRequested = true;
                            if (playback.current === resume.source)
                                playback.current = null;
                        }
                    }
                    setSeekAttempt((value) => value + 1);
                    waiting("seeking");
                });
                listen("seeked", () => {
                    const restoring =
                        progressSeek ||
                        (resume && !resume.positionApplied) ||
                        (!manualPosition.current &&
                            resumeAppliedPlayer.current === player &&
                            knownPosition.current !== null &&
                            Math.abs(
                                player!.currentTime -
                                    savedVideoResumeTime(
                                        knownPosition.current,
                                        player!.duration,
                                    ),
                            ) < 0.05);
                    observeResumePosition();
                    if (
                        !restoring &&
                        usable &&
                        player &&
                        (player.currentTime > 0 || watchStarted || userSeek)
                    )
                        startWatching();
                    userSeek = false;
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
                            showResumedNotice();
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
                const seekIntent = (event: Event) => {
                    const path = event.composedPath();
                    const timeline = path.some(
                        (node) =>
                            node instanceof Element &&
                            node.matches(
                                ".movi-progress-bar, [part~=progress-bar]",
                            ),
                    );
                    const key =
                        event instanceof KeyboardEvent &&
                        [
                            "ArrowLeft",
                            "ArrowRight",
                            "Home",
                            "End",
                            "j",
                            "l",
                        ].includes(event.key) &&
                        !path.some(
                            (node) =>
                                node instanceof Element &&
                                node.matches(
                                    "input, textarea, select, [contenteditable], [role=menu]",
                                ),
                        );
                    if (!timeline && !key) return;
                    userSeek = true;
                    manualPosition.current = true;
                    resumedPosition.current = null;
                    setResumedTime(null);
                    cancelProgressSeek?.();
                };
                player.addEventListener("pointerdown", seekIntent, {
                    capture: true,
                    signal: posterController.signal,
                });
                player.addEventListener("keydown", seekIntent, {
                    capture: true,
                    signal: posterController.signal,
                });
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
                        showResumedNotice();
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
            window.removeEventListener("pagehide", closeProgress);
            flushPosition();
            if (flushProgress.current === flushPosition)
                flushProgress.current = null;
            if (seekSavedPosition.current === seekSaved)
                seekSavedPosition.current = null;
            if (closingPosition.current === snapshotClosingPosition)
                closingPosition.current = null;
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
    useLayoutEffect(
        () => () => {
            // Capture before custom-element disconnection resets its clock to zero.
            finalizeProgress();
            progressActive.current = false;
        },
        [fileId],
    );
    const copy =
        state.phase === "buffering" && transfer?.decodeBound
            ? "Waiting for video decoding…"
            : state.phase === "buffering" && transfer?.deliveryWaiting
              ? "Waiting for video data…"
              : state.phase !== "ready"
                ? phaseCopy[state.phase]
                : null;
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
        progressDecision === "choice" && state.ready && !playbackError ? (
            <div className="advanced-state-actions advanced-continue">
                {continueTime !== null &&
                    savedVideoResumeTime(
                        continueTime,
                        activePlayer.current?.duration ?? 0,
                    ) > 0 && (
                        <button
                            type="button"
                            onClick={() => {
                                const player = activePlayer.current;
                                if (!player) return;
                                player.focus({ preventScroll: true });
                                const target = savedVideoResumeTime(
                                    continueTime,
                                    player.duration,
                                );
                                manualPosition.current = true;
                                resumeAppliedPlayer.current = player;
                                decide("pending");
                                setContinueTime(null);
                                const generation = progressGeneration.current;
                                void seekSavedPosition
                                    .current?.(target, true)
                                    .then((applied) => {
                                        if (
                                            applied &&
                                            progressGeneration.current ===
                                                generation &&
                                            activePlayer.current === player
                                        )
                                            acceptCurrentProgress(true);
                                    });
                            }}
                        >
                            <Play size={16} aria-hidden="true" /> Continue from{" "}
                            {formatVideoTime(
                                savedVideoResumeTime(
                                    continueTime,
                                    activePlayer.current?.duration ?? 0,
                                ),
                            )}
                        </button>
                    )}
                <button
                    type="button"
                    onClick={() => {
                        activePlayer.current?.focus({ preventScroll: true });
                        acceptCurrentProgress(true);
                    }}
                >
                    Keep playing from here
                </button>
            </div>
        ) : null;
    const resumedNotice =
        resumedTime !== null && !playbackError ? (
            <div
                className="advanced-state-actions advanced-continue advanced-resumed"
                role="status"
                onFocusCapture={() => setResumeNoticeFocused(true)}
                onBlurCapture={(event) => {
                    if (!event.currentTarget.contains(event.relatedTarget))
                        setResumeNoticeFocused(false);
                }}
            >
                <span>Resumed at {formatVideoTime(resumedTime)}</span>
                <button
                    type="button"
                    onClick={() => {
                        const player = activePlayer.current;
                        if (!player) return;
                        player.focus({ preventScroll: true });
                        const playing = !player.paused;
                        manualPosition.current = true;
                        decide("pending");
                        setResumedTime(null);
                        const generation = progressGeneration.current;
                        void seekSavedPosition
                            .current?.(0, false)
                            .then((applied) => {
                                if (
                                    !applied ||
                                    progressGeneration.current !== generation ||
                                    activePlayer.current !== player
                                )
                                    return;
                                acceptCurrentProgress(true, 0);
                                if (playing) void player.play().catch(() => {});
                            });
                    }}
                >
                    Start over
                </button>
            </div>
        ) : null;
    const overlays = (
        <>
            {loadingStatus}
            {continueButton}
            {resumedNotice}
            {progressNotice && (
                <p className="advanced-progress-notice" role="status">
                    {progressNotice}
                    {progressDecision === "failed" &&
                        progressMode !== null &&
                        progressMode !== "unavailable" && (
                            <button
                                type="button"
                                className="text-button"
                                onClick={() => {
                                    activePlayer.current?.focus({
                                        preventScroll: true,
                                    });
                                    setProgressAttempt((value) => value + 1);
                                }}
                            >
                                Retry saved position
                            </button>
                        )}
                </p>
            )}
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
