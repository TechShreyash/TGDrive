import { useEffect, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { LoaderCircle, RefreshCw } from "lucide-react";
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
          throw new Error("Unexpected enhanced-player style content.");
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
  phase: "player" | "opening" | "preparing" | "ready" | "buffering" | "seeking";
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
      return state.phase === "ready" ? state : { phase: "ready", ready: true };
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
export type VideoChapter = { title: string; start: number };
/** Container metadata is enough to offer chapters; never wait for remote artwork. */
export function videoChapters(
  chapters: VideoChapter[],
  duration: number,
): VideoChapter[] {
  return chapters
    .filter(
      (chapter) =>
        Number.isFinite(chapter.start) &&
        chapter.start >= 0 &&
        (!Number.isFinite(duration) ||
          duration <= 0 ||
          chapter.start < duration),
    )
    .map((chapter, index) => ({
      start: chapter.start,
      title: chapter.title.trim() || "Chapter " + (index + 1),
    }));
}
export function chapterTime(time: number): string {
  const seconds = Math.max(0, Math.floor(time));
  const minutes = Math.floor(seconds / 60);
  return (
    (minutes >= 60
      ? Math.floor(minutes / 60) + ":" + String(minutes % 60).padStart(2, "0")
      : String(minutes)) +
    ":" +
    String(seconds % 60).padStart(2, "0")
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
      tracks?: { type?: string; codec?: string; isAttachedPic?: boolean }[];
    } | null;
  } | null;
  if (player?.videoDecoder?.configured !== false) return null;
  const video = player
    .getMediaInfo?.()
    ?.tracks?.find((track) => track.type === "video" && !track.isAttachedPic);
  if (!video) return null;
  return video.codec && /^[a-z0-9_.-]{1,48}$/i.test(video.codec)
    ? video.codec
    : "unknown";
}
export type VideoBufferSnapshot = {
  ranges: { start: number; end: number }[];
  received: number;
  bytesPerSecond: number;
};
/** Movi's source-window time ranges are estimates, not decoded readiness. */
export function videoBufferSnapshot(player: {
  duration: number;
  currentTime: number;
  buffered: TimeRanges;
  player?: { getSource?: () => unknown } | null;
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
  src,
  name,
  playback,
  qualityControl,
}: {
  src: string;
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
        if (document.fullscreenElement === activePlayer.current && surface) {
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
      setVideoQualityControl(activePlayer.current, currentQualityControl());
  }, [qualityControl]);
  const [state, dispatch] = useReducer(
    videoLoadTransition,
    initialVideoLoadState,
  );
  const [attempt, setAttempt] = useState(0);
  const [slow, setSlow] = useState(false);
  const [showStatus, setShowStatus] = useState(false);
  const [playbackError, setPlaybackError] = useState("");
  const [visible, setVisible] = useState(!document.hidden);
  const [chapterSurface, setChapterSurface] = useState<ShadowRoot | null>(null);
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
      setChapterSurface(
        document.fullscreenElement === activePlayer.current
          ? activePlayer.current?.shadowRoot || null
          : null,
      );
    };
    document.addEventListener("fullscreenchange", update);
    return () => document.removeEventListener("fullscreenchange", update);
  }, []);
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
    let previousTime = Number.NaN;
    let latestProgressAt = 0;
    let chapterEntries: VideoChapter[] = [];
    let chapterRegistered = false;
    let detachPresentation: (() => void) | undefined;
    let detachPip: (() => void) | undefined;
    let detachGestures: (() => void) | undefined;
    const hideChapters = () => player?.hideOverlay("tgdrive-chapters");
    const showChapters = () => {
      if (!player || cancelled) return;
      if (player.isOverlayVisible("tgdrive-chapters")) {
        hideChapters();
        return;
      }
      const panel = document.createElement("section");
      panel.className = "advanced-chapters";
      panel.setAttribute("aria-label", "Video chapters");
      const header = document.createElement("header");
      const title = document.createElement("h3");
      title.textContent = "Chapters";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "advanced-chapters-close";
      close.textContent = "Close";
      close.setAttribute("aria-label", "Close chapters");
      close.addEventListener("click", hideChapters);
      header.append(title, close);
      const nav = document.createElement("nav");
      nav.setAttribute("aria-label", "Choose a chapter");
      for (const chapter of chapterEntries) {
        const button = document.createElement("button");
        button.type = "button";
        const label = document.createElement("span");
        label.textContent = chapter.title;
        const time = document.createElement("time");
        time.textContent = chapterTime(chapter.start);
        button.append(label, time);
        button.addEventListener("click", () => {
          if (!player) return;
          player.currentTime = chapter.start;
          hideChapters();
          player.focus({ preventScroll: true });
        });
        nav.append(button);
      }
      panel.append(header, nav);
      panel.addEventListener("click", (event) => event.stopPropagation());
      panel.addEventListener("keydown", (event) => {
        event.stopPropagation();
        if (event.key === "Escape") {
          event.preventDefault();
          hideChapters();
          player?.focus({ preventScroll: true });
        }
      });
      player.showOverlay({
        id: "tgdrive-chapters",
        content: panel,
        placement: "bottom-end",
        dismissOn: ["seek", "escape"],
        onDismiss: () =>
          player?.updateControl("tgdrive-chapters", { active: false }),
      });
      nav.querySelector("button")?.focus({ preventScroll: true });
    };
    const updateChapters = () => {
      if (!player || cancelled) return;
      chapterEntries = videoChapters(
        player.player?.getChapters() || [],
        player.duration,
      );
      if (!chapterEntries.length) {
        if (chapterRegistered) player.removeControl("tgdrive-chapters");
        chapterRegistered = false;
        hideChapters();
        refreshVideoActions(player);
        return;
      }
      if (!chapterRegistered) {
        chapterRegistered = true;
        player.addControl({
          id: "tgdrive-chapters",
          label: "Chapters",
          title: null,
          icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18M3 12h7M3 19h7"/><path d="m16 11 5 4-5 4z"/></svg>',
          placement: "both",
          side: "right",
          before: ["audio", "cc", "settings"],
          toggle: true,
          onSelect: showChapters,
        });
      }
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
    const updateBuffer = () => {
      if (!player || cancelled || failed) return;
      const sample = videoBufferSnapshot(player);
      // Pinned Movi 0.4.1 can leave this documented CSS part at zero while
      // paused, even with a ready buffer. Paint its own public TimeRanges,
      // including the seek origin, instead of converting server byte offsets.
      const bar = player.shadowRoot?.querySelector<HTMLElement>(
        '[part~="progress-buffer"]',
      );
      const range = sample.ranges[0]; // This engine exposes one contiguous source window.
      if (bar && Number.isFinite(player.duration) && player.duration > 0) {
        bar.title = "Loaded video (approximate)";
        bar.style.left =
          (range ? (range.start / player.duration) * 100 : 0) + "%";
        bar.style.width =
          (range ? ((range.end - range.start) / player.duration) * 100 : 0) +
          "%";
      }
    };
    const ready = (
      event: "loadedmetadata" | "loadeddata" | "canplay" | "ready" | "playing",
    ) => {
      if (cancelled || codecRejected) return;
      if (failed && (event === "loadedmetadata" || event === "loadeddata"))
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
    };
    const waiting = (kind: "waiting" | "seeking") => {
      if (cancelled || failed) return;
      if (!usable) {
        dispatch("trackschange");
        return;
      }
      if (kind === "waiting" && player?.paused) return;
      if (kind === "seeking" && player) {
        pendingSeek = true;
        previousTime = Number.NaN;
        latestProgressAt = 0;
      }
      pendingWait = pendingSeek ? "seeking" : kind;
      if (waitingTimer !== undefined) return;
      waitingTimer = window.setTimeout(() => {
        waitingTimer = undefined;
        if (cancelled || failed || !player) return;
        if (
          pendingWait === "waiting" &&
          (player.paused || performance.now() - latestProgressAt < 500)
        )
          return;
        dispatch(pendingWait);
      }, 700);
    };
    const settle = (event: "pause" | "seeked" | "ended") => {
      if (cancelled || failed) return;
      clearWaiting();
      if (event !== "seeked") pendingSeek = false;
      dispatch(event);
    };
    void import("movi-player/element/slim")
      .then(({ MoviElement }) => {
        if (cancelled || !host.current) return;
        installLocalStyles(MoviElement);
        installVideoPresentation(MoviElement);
        player = document.createElement("movi-player");
        activePlayer.current = player;
        setVideoQualityControl(player, currentQualityControl());
        detachPresentation = attachVideoPresentation(player);
        detachPip = installVideoPlayerPip(player);
        detachGestures = attachVideoGestures(player);
        player.setAttribute("wasmurl", wasmUrl);
        player.setAttribute("engine", "wasm");
        player.setAttribute("sw", "auto");
        player.setAttribute("buffersize", "32");
        player.setAttribute("persist", "");
        player.setAttribute("controls", "");
        player.setAttribute("playsinline", "");
        player.setAttribute("preload", "metadata");
        player.setAttribute("theme", "dark");
        player.setAttribute("themecolor", "#647dff");
        player.setAttribute("fastseek", "keys gestures");
        // Chapter titles and times work immediately, even on a slow Telegram link.
        // notimeline disables thumbnail generation, not the normal seek/progress bar.
        player.setAttribute("controlslist", "notimeline nospinner");
        player.setAttribute("aria-label", "Enhanced video player for " + name);
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
        }
        const listen = (type: string, callback: EventListener) => {
          player!.addEventListener(type, callback);
          listeners.push([type, callback]);
        };
        listen("loadstart", () => {
          codecRejected = false;
          recover();
          usable = false;
          clearWaiting();
          dispatch("loadstart");
        });
        listen("trackschange", () => {
          updateChapters();
          dispatch("trackschange");
        });
        // TrackManager emits this synchronously before choosing the decoder.
        // Install the public extension in time to receive raw MP4 text packets.
        listen("subtitletrackchange", updateSubtitleRenderer);
        listen("loadedmetadata", () => ready("loadedmetadata"));
        listen("loadeddata", () => ready("loadeddata"));
        listen("canplay", () => ready("canplay"));
        listen("playing", () => ready("playing"));
        listen("waiting", () => waiting("waiting"));
        listen("stalled", () => waiting("waiting"));
        listen("seeking", () => waiting("seeking"));
        listen("seeked", () => {
          observeResumePosition();
          settle("seeked");
          // A new position can be available before its audio/video refill is ready.
          // Keep the actual engine wait visible instead of treating seeked as play.
          if (player?.player?.getState() === "buffering") waiting("waiting");
          else pendingSeek = false;
        });
        const observeProgress = () => {
          if (!player || cancelled) return;
          observeResumePosition();
          updateBuffer();
        };
        listen("timeupdate", observeProgress);
        listen("progress", observeProgress);
        listen("durationchange", updateStartPosition);
        listen("pause", () => settle("pause"));
        listen("ended", () => settle("ended"));
        listen("statechange", (event) => {
          const phase = (event as CustomEvent<string>).detail;
          if (phase === "ready" || (phase === "paused" && player!.duration > 0))
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
          const mediaTime = (metadata as { mediaTime?: number } | null)
            ?.mediaTime;
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
            pendingSeek = false;
            latestProgressAt = performance.now();
            clearWaiting();
            dispatch("playing");
          }
          previousTime = mediaTime ?? Number.NaN;
          frameCallback = player.requestVideoFrameCallback(presentedFrame);
        };
        frameCallback = player.requestVideoFrameCallback(presentedFrame);
        progressTimer = window.setInterval(observeProgress, 250);
      })
      .catch(fail);
    return () => {
      cancelled = true;
      clearWaiting();
      clearInterval(progressTimer);
      detachPresentation?.();
      detachPip?.();
      detachGestures?.();
      if (player) {
        restorePlayerFocus.current ||= document.activeElement === player;
        clearVideoQualityControl(player);
        if (frameCallback !== undefined)
          player.cancelVideoFrameCallback(frameCallback);
        if (resume && !resume.positionApplied)
          playback.current = {
            time: resume.target,
            playing: usable ? !player.paused : resume.source.playing,
          };
        else if (usable && Number.isFinite(player.currentTime))
          playback.current = {
            time: videoResumeTime(player.currentTime, player.duration),
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
        if (activePlayer.current === player) activePlayer.current = null;
      }
    };
  }, [src, name, playback, attempt]);
  const copy = state.phase !== "ready" ? phaseCopy[state.phase] : null;
  const loadingStatus =
    playbackError || (copy && showStatus) ? (
      <div
        className={
          "advanced-state " +
          (playbackError ? "advanced-state-error" : "advanced-state-buffering")
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
                <span role="progressbar" aria-label="Loading video">
                  <LoaderCircle size={18} aria-hidden="true" />
                </span>
                <span>{copy}</span>
              </div>
            )}
          </div>
          {!playbackError && slow && (
            <p className="advanced-wait-help">
              Taking longer than usual. You can wait or try again.
            </p>
          )}
          {(slow || playbackError) && (
            <div className="advanced-state-actions">
              <button
                type="button"
                onClick={() => setAttempt((value) => value + 1)}
              >
                <RefreshCw size={16} aria-hidden="true" /> Try again
              </button>
            </div>
          )}
        </div>
      </div>
    ) : null;
  return (
    <div className="advanced-video" data-phase={state.phase}>
      <div
        ref={host}
        className="advanced-player-host"
        aria-busy={!state.ready && !playbackError}
      />
      {chapterSurface
        ? createPortal(loadingStatus, chapterSurface)
        : loadingStatus}
    </div>
  );
}
