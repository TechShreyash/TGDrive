import { useEffect, useRef, useState } from "react";
import { errorMessage } from "../../../shared/api";
import { Spinner } from "../../../shared/components";
import { loadBookImage, type ImageProgress } from "./bookImage";

export default function BookImage({
    id,
    src,
    alt,
    eager = false,
}: {
    id?: string;
    src: string;
    alt: string;
    eager?: boolean;
}) {
    const container = useRef<HTMLSpanElement>(null);
    const [visible, setVisible] = useState(eager);
    const [placeholderHeight, setPlaceholderHeight] = useState(48);
    const [attempt, setAttempt] = useState(0);
    const [url, setUrl] = useState("");
    const [ready, setReady] = useState(false);
    const [error, setError] = useState("");
    const [progress, setProgress] = useState<ImageProgress>({
        received: 0,
        total: 0,
        attempt: 1,
        retrying: false,
    });
    useEffect(() => {
        if (eager) {
            setVisible(true);
            return;
        }
        if (!container.current) return;
        const observer = new IntersectionObserver(
            (entries) => {
                const entry = entries[0];
                if (!entry.isIntersecting)
                    setPlaceholderHeight(entry.boundingClientRect.height);
                setVisible(entry.isIntersecting);
            },
            { rootMargin: "200px" },
        );
        observer.observe(container.current);
        return () => observer.disconnect();
    }, [eager]);
    useEffect(() => {
        if (!visible) {
            setUrl("");
            setReady(false);
            return;
        }
        const controller = new AbortController();
        let objectUrl = "";
        setUrl("");
        setReady(false);
        setError("");
        void loadBookImage(src, controller.signal, setProgress)
            .then((blob) => {
                if (controller.signal.aborted) return;
                objectUrl = URL.createObjectURL(blob);
                setUrl(objectUrl);
            })
            .catch((cause) => {
                if (!controller.signal.aborted) setError(errorMessage(cause));
            });
        return () => {
            controller.abort();
            if (objectUrl) URL.revokeObjectURL(objectUrl);
        };
    }, [src, attempt, visible]);
    const amount = (progress.received / 1024 / 1024).toFixed(1) + " MiB";
    return (
        <span
            id={id}
            ref={container}
            className="book-image"
            aria-busy={visible && !ready && !error}
            style={
                !eager && (!visible || !ready)
                    ? { minHeight: placeholderHeight }
                    : undefined
            }
        >
            {visible && !ready && !error && (
                <span className="book-image-status" role="status">
                    <Spinner />
                    {progress.retrying
                        ? "Connection interrupted. Retrying page…"
                        : url
                          ? "Displaying page…"
                          : "Loading page…"}
                    {progress.received > 0 && (
                        <span>
                            {amount}
                            {progress.total > 0
                                ? " / " +
                                  (progress.total / 1024 / 1024).toFixed(1) +
                                  " MiB"
                                : " received"}
                        </span>
                    )}
                </span>
            )}
            {visible && error && (
                <span className="book-image-error" role="alert">
                    <span>{alt}</span>
                    <span>{error}</span>
                    <button
                        className="text-button"
                        onClick={() => setAttempt((n) => n + 1)}
                    >
                        Retry image
                    </button>
                </span>
            )}
            {visible && url && !error && (
                <img
                    src={url}
                    alt={alt}
                    hidden={!ready}
                    decoding="async"
                    onLoad={() => setReady(true)}
                    onError={() =>
                        setError(
                            "The browser could not decode this image. Download the original to open it in another reader.",
                        )
                    }
                />
            )}
        </span>
    );
}
