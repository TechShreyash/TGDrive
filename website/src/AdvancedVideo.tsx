import { useEffect, useReducer, useRef, useState } from "react";
import { RefreshCw, Play } from "lucide-react";
import type { MoviElement } from "movi-player/element/slim";
import wasmUrl from "movi-player/movi.wasm?url";
import { Spinner } from "./components";
import MovTextRenderer, { isMovTextCodec } from "./MovTextRenderer";
import "./AdvancedVideo.css";

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
      "button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:2px solid #b9c6ff!important;outline-offset:3px!important}",
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
      return { phase: "ready", ready: true };
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
const phaseCopy = {
  player: {
    title: "Loading player",
    description: "Loading the playback tools for this video.",
  },
  opening: {
    title: "Opening video",
    description: "Reading the video and its available tracks.",
  },
  preparing: {
    title: "Preparing playback",
    description: "Preparing the video for playback.",
  },
  buffering: {
    title: "Buffering video",
    description: "Waiting for more video data. Playback will continue shortly.",
  },
  seeking: {
    title: "Moving to this position",
    description: "Loading the video at the selected time.",
  },
};
export default function AdvancedVideo({
  src,
  name,
  onFallback,
  onUnsupported,
  playback,
}: {
  src: string;
  name: string;
  onFallback: () => void;
  onUnsupported: (codec: string) => void;
  playback: { current: { time: number; playing: boolean } | null };
}) {
  const host = useRef<HTMLDivElement>(null);
  const activePlayer = useRef<MoviElement | null>(null);
  const [state, dispatch] = useReducer(
    videoLoadTransition,
    initialVideoLoadState,
  );
  const [attempt, setAttempt] = useState(0);
  const [slow, setSlow] = useState(false);
  const [engineAvailable, setEngineAvailable] = useState(false);
  const [playRequested, setPlayRequested] = useState(false);
  const [playMessage, setPlayMessage] = useState("");
  const [visible, setVisible] = useState(!document.hidden);
  const fallback = useRef(onFallback);
  fallback.current = onFallback;
  const unsupported = useRef(onUnsupported);
  unsupported.current = onUnsupported;
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
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
    let usable = false;
    let player: MoviElement | null = null;
    let subtitleRenderer: MovTextRenderer | null = null;
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
    let pendingWait: "waiting" | "seeking" = "waiting";
    const listeners: [string, EventListener][] = [];
    dispatch("reset");
    setEngineAvailable(false);
    setPlayRequested(false);
    setPlayMessage("");
    const clearWaiting = () => {
      clearTimeout(waitingTimer);
      waitingTimer = undefined;
    };
    const fail = () => {
      if (!cancelled && !failed) {
        failed = true;
        clearWaiting();
        fallback.current();
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
    const ready = (
      event: "loadedmetadata" | "loadeddata" | "canplay" | "ready" | "playing",
    ) => {
      if (cancelled || failed) return;
      if (event === "canplay" || event === "ready") {
        const codec = unavailableVideoCodec(player?.player);
        if (codec) {
          failed = true;
          clearWaiting();
          unsupported.current(codec);
          return;
        }
      }
      usable = true;
      updateSubtitleRenderer();
      clearWaiting();
      dispatch(event);
      setPlayRequested(false);
      setPlayMessage("");
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
      pendingWait = kind;
      if (waitingTimer !== undefined) return;
      waitingTimer = window.setTimeout(() => {
        waitingTimer = undefined;
        if (!cancelled && !failed) dispatch(pendingWait);
      }, 180);
    };
    const settle = (event: "pause" | "seeked" | "ended") => {
      if (cancelled || failed) return;
      clearWaiting();
      dispatch(event);
    };
    void import("movi-player/element/slim")
      .then(({ MoviElement }) => {
        if (cancelled || !host.current) return;
        installLocalStyles(MoviElement);
        player = document.createElement("movi-player");
        activePlayer.current = player;
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
        player.setAttribute("nohotkeys", "");
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
          usable = false;
          clearWaiting();
          dispatch("loadstart");
        });
        listen("trackschange", () => dispatch("trackschange"));
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
        });
        listen("timeupdate", observeResumePosition);
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
        setEngineAvailable(true);
        player.src = new URL(src, window.location.origin).href;
      })
      .catch(fail);
    return () => {
      cancelled = true;
      clearWaiting();
      if (player) {
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
  return (
    <div className="advanced-video" data-phase={state.phase}>
      <div
        ref={host}
        className="advanced-player-host"
        inert={!state.ready}
        aria-hidden={!state.ready}
        aria-busy={!state.ready}
      />
      {copy && (
        <div
          className={
            "advanced-state" + (state.ready ? " advanced-state-buffering" : "")
          }
          data-phase={state.phase}
          data-hidden={!visible}
        >
          <div className="advanced-state-content">
            <div role="status" aria-live="polite" aria-atomic="true">
              <div className="advanced-state-indicator" aria-hidden="true">
                <Spinner />
              </div>
              <h3>{copy.title}</h3>
              <p>
                {playMessage ||
                  (playRequested
                    ? "Playback will start when the video is ready."
                    : slow
                      ? "This is taking longer than usual. You can keep waiting or reload the video."
                      : copy.description)}
              </p>
            </div>
            {slow && (
              <div className="advanced-state-actions">
                {!state.ready && engineAvailable && (
                  <button
                    type="button"
                    className="advanced-start"
                    disabled={playRequested}
                    onClick={() => {
                      const current = activePlayer.current;
                      if (!current) return;
                      setPlayRequested(true);
                      setPlayMessage("");
                      void current.play().catch(() => {
                        if (activePlayer.current !== current) return;
                        setPlayRequested(false);
                        setPlayMessage(
                          "Playback cannot start yet. Keep waiting or reload the video.",
                        );
                      });
                    }}
                  >
                    <Play size={16} />
                    {playRequested ? "Play requested" : "Start Playback"}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setAttempt((value) => value + 1)}
                >
                  <RefreshCw size={16} /> Reload Video
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
