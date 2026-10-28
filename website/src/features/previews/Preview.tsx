import AdvancedVideo from "./video/AdvancedVideo";
import { videoPosterUrl } from "./video/videoPoster";
import "./Preview.css";
import "./Reader.css";
import { readBoundedResponse, PreviewLimitError } from "./previewIO";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
    AlertCircle,
    ArrowLeft,
    ChevronDown,
    ChevronUp,
    Download,
    Maximize,
    Minimize,
    RefreshCw,
    X,
} from "lucide-react";
import { request, errorMessage, sessionExpiryHandler } from "../../shared/api";
import {
    itemPath,
    fileUrl,
    formatBytes,
    formatDate,
    mediaRequestId,
    type DriveItem,
    type PreviewDescriptor,
} from "../../shared/types";
import { FileIcon, Spinner } from "../../shared/components";

function ViewerUnavailable() {
    return (
        <div className="empty-state">
            <AlertCircle size={34} />
            <h3>Viewer could not load</h3>
            <p>
                Check your connection and reload the viewer, or download the
                original.
            </p>
            <button
                className="secondary-button"
                onClick={() => window.location.reload()}
            >
                <RefreshCw size={16} /> Reload Viewer
            </button>
        </div>
    );
}
const PdfPreview = lazy(() =>
    import("./pdf/PdfPreview").catch(() => ({ default: ViewerUnavailable })),
);

const BookPreview = lazy(() =>
    import("./books/BookPreview").catch(() => ({ default: ViewerUnavailable })),
);

export function nativePreviewKind(
    name: string,
): "image" | "video" | "audio" | null {
    const extension = name.split(".").at(-1)?.toLowerCase() || "";
    if (
        [
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "avif",
            "bmp",
            "ico",
            "svg",
        ].includes(extension)
    )
        return "image";
    if (
        [
            "mp4",
            "mkv",
            "webm",
            "mov",
            "ogv",
            "m4v",
            "avi",
            "mpeg",
            "mpg",
            "3gp",
            "m2ts",
            "flv",
            "wmv",
            "vob",
        ].includes(extension)
    )
        return "video";
    if (
        ["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "weba"].includes(
            extension,
        )
    )
        return "audio";
    return null;
}
export function displayText(text: string, language?: string): string {
    if (language?.toLowerCase() === "json") {
        try {
            const formatted = JSON.stringify(JSON.parse(text), null, 2);
            return formatted.length <= 524288 ? formatted : text;
        } catch {
            /* Keep incomplete/invalid JSON readable. */
        }
    }
    return text;
}
/** Bound even an unexpectedly large proxy response; never interpret file text as markup. */
export async function loadPreview(
    path: string,
    auth: string,
    signal: AbortSignal,
): Promise<PreviewDescriptor> {
    const expireSession = sessionExpiryHandler();
    const params = new URLSearchParams({ path });
    if (auth) params.set("auth", auth);
    const response = await fetch("/api/v1/preview?" + params, {
        credentials: "same-origin",
        signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
        headers: { Accept: "application/json" },
    });
    expireSession(response.status);
    let content: string;
    try {
        content = new TextDecoder().decode(
            await readBoundedResponse(response, 2 * 1024 * 1024, signal),
        );
    } catch (error) {
        if (error instanceof PreviewLimitError)
            throw new Error(
                "This preview is too large to display. Download the original instead.",
            );
        throw error;
    }
    let data: PreviewDescriptor & { detail?: string };
    try {
        data = JSON.parse(content);
    } catch {
        throw new Error(
            "The server returned an unreadable preview. Try Again.",
        );
    }
    if (!response.ok) {
        throw new Error(
            typeof data?.detail === "string"
                ? data.detail
                : "This preview could not be loaded. Try Again.",
        );
    }
    if (
        !data ||
        typeof data !== "object" ||
        !["text", "pdf", "video", "book", "unsupported"].includes(data.kind) ||
        (data.size !== undefined &&
            (!Number.isSafeInteger(data.size) || data.size < 0)) ||
        (data.reason !== undefined &&
            (typeof data.reason !== "string" || data.reason.length > 2000)) ||
        (data.language !== undefined && typeof data.language !== "string") ||
        (data.truncated !== undefined && typeof data.truncated !== "boolean") ||
        (data.url !== undefined && typeof data.url !== "string") ||
        (data.kind === "text" &&
            (typeof data.text !== "string" || data.text.length > 262144))
    )
        throw new Error(
            "The server returned an invalid preview. Download the original instead.",
        );
    return data;
}
function safePdfUrl(value?: string): string | undefined {
    if (!value) return;
    try {
        const url = new URL(value, window.location.origin);
        if (
            url.origin === window.location.origin &&
            url.pathname === "/file" &&
            url.searchParams.get("preview") === "1"
        )
            return url.pathname + url.search;
    } catch {
        /* An invalid descriptor cannot become an iframe navigation. */
    }
}
type FileRequestStatus = {
    queue?: {
        position: number;
        estimated_wait_seconds: number | null;
    } | null;
    phase?:
        | "checking_file"
        | "waiting_for_telegram"
        | "receiving"
        | "retry_wait"
        | "error"
        | null;
    retry_after?: number | null;
    code?: string | null;
};
export function previewRetryDelay(status: FileRequestStatus, retries: number) {
    if (
        retries >= 2 ||
        status.code !== "TELEGRAM_QUEUE_FULL" ||
        status.phase !== "retry_wait"
    )
        return null;
    const seconds = status.retry_after ?? 1;
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 2147483)
        return null;
    return Math.max(1000, Math.ceil(seconds * 1000));
}
export default function Preview({
    item,
    auth,
    onClose,
}: {
    item: DriveItem;
    auth: string;
    onClose?: () => void;
}) {
    const nativeKind = nativePreviewKind(item.name);
    const path = itemPath(item);
    const viewer = useRef<HTMLDialogElement>(null);
    const fullscreenSurface = useRef<HTMLDivElement>(null);
    const [fullscreen, setFullscreen] = useState(false);
    const [toolbarCollapsed, setToolbarCollapsed] = useState(false);
    const [fullscreenError, setFullscreenError] = useState("");
    useEffect(() => {
        const surface = fullscreenSurface.current;
        let wasFullscreen = false;
        const update = () => {
            const fullscreenElement = document.fullscreenElement;
            const active = fullscreenElement === surface;
            const containsFullscreen = Boolean(
                fullscreenElement && surface?.contains(fullscreenElement),
            );
            const exited = wasFullscreen && !containsFullscreen;
            wasFullscreen = containsFullscreen;
            setFullscreen(active);
            setToolbarCollapsed(false);
            if (active || exited)
                requestAnimationFrame(() =>
                    surface
                        ?.querySelector<HTMLButtonElement>(
                            active
                                ? ".preview-toolbar-toggle"
                                : ".preview-fullscreen",
                        )
                        ?.focus({ preventScroll: true }),
                );
        };
        document.addEventListener("fullscreenchange", update);
        return () => {
            document.removeEventListener("fullscreenchange", update);
            if (
                document.fullscreenElement &&
                surface?.contains(document.fullscreenElement)
            )
                void document.exitFullscreen().catch(() => {});
        };
    }, []);
    async function toggleFullscreen() {
        setFullscreenError("");
        try {
            const surface = fullscreenSurface.current;
            const target =
                kind === "video"
                    ? surface?.querySelector<HTMLElement>("movi-player")
                    : surface;
            if (
                document.fullscreenElement &&
                surface?.contains(document.fullscreenElement)
            )
                await document.exitFullscreen();
            else if (kind === "video" && !target)
                setFullscreenError(
                    "The video player is still loading. Try fullscreen again in a moment.",
                );
            else if (document.fullscreenEnabled && target?.requestFullscreen) {
                await target.requestFullscreen();
                if (kind === "video") target.focus({ preventScroll: true });
            } else
                setFullscreenError(
                    "Fullscreen is not available in this browser.",
                );
        } catch {
            setFullscreenError(
                "Fullscreen could not open. Try again or use your browser's fullscreen control.",
            );
        }
    }
    const [fileStatus, setFileStatus] = useState<FileRequestStatus | null>(null);
    const [toolbarTarget, setToolbarTarget] = useState<HTMLDivElement | null>(
        null,
    );
    useEffect(() => {
        const node = viewer.current!;
        const trigger = document.activeElement as HTMLElement | null;
        const previousOverflow = document.body.style.overflow;
        const position = { x: window.scrollX, y: window.scrollY };
        document.body.style.overflow = "hidden";
        node.showModal();
        node.querySelector<HTMLElement>(
            ".preview-close, .preview-download, .file-details-download",
        )?.focus();
        return () => {
            node.querySelectorAll<HTMLMediaElement>("video,audio").forEach(
                (media) => {
                    media.pause();
                    media.removeAttribute("src");
                    media.load();
                },
            );
            node.close();
            document.body.style.overflow = previousOverflow;
            requestAnimationFrame(() => {
                if (document.querySelector(".file-preview[open]")) return;
                const row = document.querySelector<HTMLElement>(
                    '[data-item-id="' +
                        CSS.escape(item.id) +
                        '"] .item-name-button',
                );
                const target =
                    trigger?.isConnected && trigger.getClientRects().length
                        ? trigger
                        : row || document.getElementById("main-content");
                target?.focus({ preventScroll: true });
                window.scrollTo(position.x, position.y);
            });
        };
    }, [item.id]);
    const [failed, setFailed] = useState(false);
    const [preview, setPreview] = useState<{
        path: string;
        value: PreviewDescriptor;
    } | null>(null);
    const descriptor = preview?.path === path ? preview.value : null;
    const kind = nativeKind || (descriptor?.kind === "video" ? "video" : null);
    useEffect(() => {
        setFileStatus(null);
        // The player's own readiness state is authoritative for video. A server
        // transfer queue can stay busy while cached video is already playing.
        if (kind === "video" || (!nativeKind && !descriptor)) return;
        const controller = new AbortController();
        const params = new URLSearchParams({
            path,
            request_id: mediaRequestId(path, auth),
        });
        if (auth) params.set("auth", auth);
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            try {
                const data = await request<FileRequestStatus>(
                    "/api/v1/file-status?" + params,
                    undefined,
                    { signal: controller.signal },
                );
                if (!controller.signal.aborted) setFileStatus(data);
            } catch {
                /* Previews remain usable if queue diagnostics are unavailable. */
            }
            if (!controller.signal.aborted) timer = setTimeout(poll, 2500);
        };
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [path, auth, kind, nativeKind, descriptor]);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(!nativeKind);
    const [attempt, setAttempt] = useState(0);
    const [wrap, setWrap] = useState(true);
    const [variants, setVariants] = useState<{ id: string; label: string }[]>(
        [],
    );
    const [quality, setQuality] = useState("original");
    const [mediaFailure, setMediaFailure] = useState<
        "checking" | "retrying" | "busy" | null
    >(null);
    const mediaRetries = useRef(0);
    useEffect(() => {
        mediaRetries.current = 0;
        setMediaFailure(null);
    }, [path, auth, quality]);
    useEffect(() => {
        if (!failed || (kind !== "image" && kind !== "audio")) return;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined;
        const params = new URLSearchParams({
            path,
            request_id: mediaRequestId(path, auth),
        });
        if (auth) params.set("auth", auth);
        void request<FileRequestStatus>(
            "/api/v1/file-status?" + params,
            undefined,
            { signal: controller.signal, timeoutMs: 10000 },
        )
            .then((status) => {
                if (controller.signal.aborted) return;
                setFileStatus(status);
                if (status.code !== "TELEGRAM_QUEUE_FULL") {
                    setMediaFailure(null);
                    return;
                }
                const delay = previewRetryDelay(status, mediaRetries.current);
                setMediaFailure(delay === null ? "busy" : "retrying");
                if (delay === null) return;
                mediaRetries.current++;
                timer = setTimeout(() => {
                    setFailed(false);
                    setMediaFailure(null);
                    setAttempt((value) => value + 1);
                }, delay);
            })
            .catch(() => {
                if (!controller.signal.aborted) setMediaFailure(null);
            });
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [failed, path, auth, kind, attempt]);
    function mediaFailed() {
        setMediaFailure("checking");
        setFailed(true);
    }
    const playback = useRef<{ time: number; playing: boolean } | null>(null);
    const src =
        fileUrl(path, auth) +
        (kind === "video" || kind === "audio" ? "&preview=1" : "") +
        (quality === "original"
            ? ""
            : "&quality=" + encodeURIComponent(quality));
    useEffect(() => {
        setVariants([]);
        setQuality("original");
        if (kind !== "image" && kind !== "video") return;
        const controller = new AbortController();
        const params = new URLSearchParams({ path });
        if (auth) params.set("auth", auth);
        void fetch("/api/v1/media?" + params, {
            credentials: "same-origin",
            signal: AbortSignal.any([
                controller.signal,
                AbortSignal.timeout(15000),
            ]),
        })
            .then(async (response) => {
                if (!response.ok) return;
                const data = await response.json();
                if (!controller.signal.aborted && Array.isArray(data.variants))
                    setVariants(
                        data.variants
                            .filter(
                                (value: { id?: string; label?: string }) =>
                                    typeof value.id === "string" &&
                                    typeof value.label === "string",
                            )
                            .slice(0, 20),
                    );
            })
            .catch(() => {
                /* Original playback is independent of optional native variants. */
            });
        return () => controller.abort();
    }, [path, auth, kind]);
    useEffect(() => {
        setFailed(false);
        if (nativeKind) {
            setLoading(false);
            return;
        }
        const controller = new AbortController();
        setLoading(true);
        setError("");
        setPreview(null);
        void loadPreview(path, auth, controller.signal)
            .then((value) => {
                if (!controller.signal.aborted) setPreview({ path, value });
            })
            .catch((error) => {
                if (!controller.signal.aborted) setError(errorMessage(error));
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [path, auth, nativeKind, attempt]);
    const pdf =
        descriptor?.kind === "pdf" ? safePdfUrl(descriptor.url) : undefined;
    const documentPreview = !!pdf || descriptor?.kind === "book";
    const details =
        !kind && !error && (loading || descriptor?.kind === "unsupported");
    const suffix = item.name.includes(".")
        ? item.name.slice(item.name.lastIndexOf(".") + 1)
        : "";
    const fileType = /^[a-z0-9]{1,12}$/i.test(suffix)
        ? suffix.toUpperCase() + " file"
        : "File";
    const queue = fileStatus?.queue;
    const waiting =
        fileStatus?.phase === "waiting_for_telegram" ||
        (queue != null && queue.position > 0);
    const waitMessage = waiting
        ? "Waiting for Telegram capacity"
        : fileStatus?.phase === "checking_file"
          ? "Checking file…"
          : fileStatus?.phase === "receiving"
            ? "Receiving file…"
            : null;
    return (
        <dialog
            ref={viewer}
            className={
                "file-preview" +
                (details ? " file-details" : "") +
                (!onClose ? " preview-embedded" : "")
            }
            aria-labelledby="preview-title"
            aria-describedby={details ? "file-details-reason" : undefined}
            onClick={(event) => {
                if (!details || event.target !== event.currentTarget) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                if (
                    event.clientX < bounds.left ||
                    event.clientX > bounds.right ||
                    event.clientY < bounds.top ||
                    event.clientY > bounds.bottom
                )
                    onClose?.();
            }}
            onCancel={(event) => {
                event.preventDefault();
                if (
                    document.fullscreenElement &&
                    fullscreenSurface.current?.contains(
                        document.fullscreenElement,
                    )
                ) {
                    void document.exitFullscreen().catch(() => {});
                    return;
                }
                onClose?.();
            }}
        >
            <div className="preview-fullscreen-surface" ref={fullscreenSurface}>
                {fullscreen && (
                    <button
                        className="preview-toolbar-toggle"
                        type="button"
                        aria-label={
                            toolbarCollapsed ? "Show Toolbar" : "Hide Toolbar"
                        }
                        title={
                            toolbarCollapsed ? "Show Toolbar" : "Hide Toolbar"
                        }
                        aria-controls="preview-header"
                        aria-expanded={!toolbarCollapsed}
                        onClick={() =>
                            setToolbarCollapsed((collapsed) => !collapsed)
                        }
                    >
                        {toolbarCollapsed ? (
                            <ChevronDown size={19} aria-hidden="true" />
                        ) : (
                            <ChevronUp size={19} aria-hidden="true" />
                        )}
                    </button>
                )}
                <header
                    id="preview-header"
                    hidden={fullscreen && toolbarCollapsed}
                    className={
                        "preview-header" +
                        (documentPreview
                            ? " preview-header-pdf preview-header-reader"
                            : "") +
                        (descriptor?.kind === "book"
                            ? " preview-header-book"
                            : "")
                    }
                >
                    {onClose && (
                        <button
                            className="preview-close icon-button"
                            type="button"
                            aria-label="Close Preview"
                            onClick={onClose}
                        >
                            {details ? <X size={21} /> : <ArrowLeft size={22} />}
                        </button>
                    )}
                    {!details && (
                        <>
                            <FileIcon name={item.name} />
                            <div className="preview-heading">
                                <h2 id="preview-title" title={item.name}>
                                    {item.name}
                                </h2>
                                <span>
                                    {formatBytes(item.size)} ·{" "}
                                    {formatDate(item.upload_date)}
                                </span>
                            </div>
                            {documentPreview && (
                                <div
                                    className="preview-toolbar-slot"
                                    ref={setToolbarTarget}
                                />
                            )}
                            <button
                                className="icon-button preview-fullscreen"
                                aria-label={
                                    fullscreen
                                        ? "Exit Fullscreen"
                                        : "Enter Fullscreen"
                                }
                                title={
                                    fullscreen
                                        ? "Exit Fullscreen"
                                        : "Enter Fullscreen"
                                }
                                onClick={() => void toggleFullscreen()}
                            >
                                {fullscreen ? (
                                    <Minimize size={19} />
                                ) : (
                                    <Maximize size={19} />
                                )}
                            </button>
                            <a
                                className="preview-download"
                                href={fileUrl(path, auth, true)}
                                download={item.name}
                                aria-label="Download Original"
                                title="Download Original"
                            >
                                <Download size={19} />
                                <span>Download</span>
                            </a>
                        </>
                    )}
                </header>
                {fullscreenError && (
                    <p className="preview-limit" role="alert">
                        {fullscreenError}
                    </p>
                )}
                {kind !== "video" && !failed && waitMessage && (
                    <p className="preview-limit" role="status">
                        {waitMessage}
                        {waiting && queue?.estimated_wait_seconds != null
                            ? " · About " +
                              Math.ceil(queue.estimated_wait_seconds) +
                              " seconds"
                            : ""}
                    </p>
                )}
                {details ? (
                    <section className="file-details-body">
                        <div className="file-details-icon">
                            <FileIcon name={item.name} />
                        </div>
                        <h2 id="preview-title">{item.name}</h2>
                        {loading ? (
                            <p
                                id="file-details-reason"
                                className="file-details-checking"
                                role="status"
                            >
                                <Spinner /> Checking preview…
                            </p>
                        ) : (
                            <p id="file-details-reason">
                                {descriptor?.reason ||
                                    "Download this file to open it on your device."}
                            </p>
                        )}
                        <dl className="file-details-metadata">
                            <div>
                                <dt>Type</dt>
                                <dd>{fileType}</dd>
                            </div>
                            <div>
                                <dt>Size</dt>
                                <dd>
                                    {formatBytes(descriptor?.size ?? item.size)}
                                </dd>
                            </div>
                            <div>
                                <dt>Added</dt>
                                <dd>{formatDate(item.upload_date)}</dd>
                            </div>
                        </dl>
                        <a
                            className="primary-button file-details-download"
                            href={fileUrl(path, auth, true)}
                            download={item.name}
                            aria-label="Download Original"
                        >
                            <Download size={19} /> Download File
                        </a>
                    </section>
                ) : (
                    <>
                        {kind !== "video" && variants.length > 0 && (
                            <label
                                className="preview-quality"
                                hidden={fullscreen && toolbarCollapsed}
                            >
                                Preview Quality
                                <select
                                    value={quality}
                                    onChange={(event) => {
                                        setFailed(false);
                                        setQuality(event.target.value);
                                    }}
                                >
                                    <option value="original">Original</option>
                                    {variants.map((value) => (
                                        <option key={value.id} value={value.id}>
                                            {value.label}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        )}
                        <div
                            className={
                                "preview-surface preview-surface-" +
                                (pdf
                                    ? "pdf"
                                    : descriptor?.kind === "text"
                                      ? "text"
                                      : kind || "other") +
                                (error || failed
                                    ? " preview-surface-message"
                                    : "")
                            }
                        >
                            {loading ? (
                                <div className="preview-loading" role="status">
                                    <Spinner /> Loading preview…
                                </div>
                            ) : error ? (
                                <div className="empty-state">
                                    <AlertCircle size={34} />
                                    <p role="alert">{error}</p>
                                    <button
                                        className="secondary-button"
                                        onClick={() =>
                                            setAttempt((value) => value + 1)
                                        }
                                    >
                                        <RefreshCw size={16} /> Try Preview
                                        Again
                                    </button>
                                </div>
                            ) : failed ? (
                                <div className="empty-state">
                                    {mediaFailure === "checking" ||
                                    mediaFailure === "retrying" ? (
                                        <Spinner />
                                    ) : (
                                        <AlertCircle size={34} />
                                    )}
                                    <h3>
                                        {mediaFailure === "checking"
                                            ? "Checking preview…"
                                            : mediaFailure
                                              ? "Telegram is busy"
                                              : "Preview Unavailable"}
                                    </h3>
                                    <p role={mediaFailure ? "status" : undefined}>
                                        {mediaFailure === "checking"
                                            ? "Checking whether Telegram is temporarily busy."
                                            : mediaFailure === "retrying"
                                              ? "Waiting for Telegram capacity. This preview will retry automatically."
                                              : mediaFailure === "busy"
                                                ? "Telegram's waiting queue is full. Try again in a moment."
                                                : "This file could not be played or displayed by your browser. Download the original to open it on your device."}
                                    </p>
                                    {mediaFailure !== "checking" &&
                                        mediaFailure !== "retrying" && (
                                            <button
                                                className="secondary-button"
                                                onClick={() => {
                                                    mediaRetries.current = 0;
                                                    setMediaFailure(null);
                                                    setFailed(false);
                                                    setAttempt(
                                                        (value) => value + 1,
                                                    );
                                                }}
                                            >
                                                <RefreshCw size={16} /> Try
                                                Preview Again
                                            </button>
                                        )}
                                </div>
                            ) : kind === "image" ? (
                                <img
                                    key={attempt}
                                    src={src}
                                    alt={item.name}
                                    onError={mediaFailed}
                                />
                            ) : kind === "video" ? (
                                <AdvancedVideo
                                    key={item.id + ":" + attempt}
                                    fileId={item.id}
                                    src={src}
                                    posterUrl={videoPosterUrl(path, auth)}
                                    name={item.name}
                                    playback={playback}
                                    qualityControl={
                                        variants.length > 0
                                            ? {
                                                  variants,
                                                  selected: quality,
                                                  onChange: (value) => {
                                                      setFailed(false);
                                                      setQuality(value);
                                                  },
                                              }
                                            : undefined
                                    }
                                />
                            ) : kind === "audio" ? (
                                <audio
                                    key={attempt}
                                    src={src}
                                    controls
                                    preload="metadata"
                                    onError={mediaFailed}
                                />
                            ) : descriptor?.kind === "text" ? (
                                <div className="text-preview">
                                    <div className="preview-document-toolbar">
                                        <span>
                                            {descriptor.language ||
                                                "Plain Text"}{" "}
                                            · Read-only
                                        </span>
                                        <label>
                                            <input
                                                type="checkbox"
                                                checked={wrap}
                                                onChange={(event) =>
                                                    setWrap(
                                                        event.target.checked,
                                                    )
                                                }
                                            />{" "}
                                            Wrap Lines
                                        </label>
                                    </div>
                                    {descriptor.truncated && (
                                        <p
                                            className="preview-limit"
                                            role="status"
                                        >
                                            Showing the first 256 KB. Download
                                            the original for the complete file.
                                        </p>
                                    )}
                                    <pre
                                        className={wrap ? "wrap-lines" : ""}
                                        tabIndex={0}
                                        aria-label={"Contents of " + item.name}
                                    >
                                        <code>
                                            {displayText(
                                                descriptor.text || "",
                                                descriptor.language,
                                            )}
                                        </code>
                                    </pre>
                                </div>
                            ) : descriptor?.kind === "book" ? (
                                <Suspense
                                    fallback={
                                        <div
                                            className="preview-loading"
                                            role="status"
                                        >
                                            <Spinner />
                                            Loading reader…
                                        </div>
                                    }
                                >
                                    <BookPreview
                                        key={path + auth}
                                        path={path}
                                        auth={auth}
                                        toolbarTarget={toolbarTarget}
                                    />
                                </Suspense>
                            ) : pdf ? (
                                <Suspense
                                    fallback={
                                        <div
                                            className="preview-loading"
                                            role="status"
                                        >
                                            <Spinner /> Loading PDF viewer…
                                        </div>
                                    }
                                >
                                    <PdfPreview
                                        url={pdf}
                                        name={item.name}
                                        size={
                                            descriptor?.size || item.size || 0
                                        }
                                        toolbarTarget={toolbarTarget}
                                    />
                                </Suspense>
                            ) : (
                                <div className="empty-state">
                                    <FileIcon name={item.name} />
                                    <h3>No Preview for This File</h3>
                                    <p>
                                        {descriptor?.reason ||
                                            "Download the original to open this format on your device."}
                                    </p>
                                </div>
                            )}
                        </div>
                    </>
                )}
            </div>
        </dialog>
    );
}
