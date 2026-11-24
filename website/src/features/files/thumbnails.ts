import { itemPath, type DriveItem } from "../../shared/types";

export const MAX_THUMBNAIL_BYTES = 128 * 1024;
export const THUMBNAIL_CONCURRENCY = 4;
export const THUMBNAIL_PENDING = 32;
const candidates = new Set([
    "mp3",
    "flac",
    "m4a",
    "ogg",
    "opus",
    "wav",
    "aac",
    "jpg",
    "jpeg",
    "png",
    "webp",
    "gif",
    "avif",
    "heic",
    "heif",
    "bmp",
    "tif",
    "tiff",
    "svg",
    "mp4",
    "mkv",
    "webm",
    "mov",
    "avi",
    "wmv",
    "m4v",
    "mpeg",
    "mpg",
    "pdf",
]);
export function thumbnailCandidate(item: DriveItem): boolean {
    return (
        item.type === "file" &&
        candidates.has(item.name.split(".").at(-1)?.toLowerCase() || "")
    );
}
export function thumbnailUrl(item: DriveItem, auth: string): string {
    return (
        "/api/v1/thumbnail?" +
        new URLSearchParams({ path: itemPath(item), ...(auth ? { auth } : {}) })
    );
}
let active = 0;
const queue: (() => void)[] = [];
function drain() {
    while (active < THUMBNAIL_CONCURRENCY && queue.length) queue.shift()!();
}
function limited<T>(signal: AbortSignal, task: () => Promise<T>): Promise<T> {
    return new Promise((resolve, reject) => {
        const cancelled = () => {
            const index = queue.indexOf(start);
            if (index >= 0) queue.splice(index, 1);
            reject(new DOMException("Thumbnail cancelled", "AbortError"));
        };
        const start = () => {
            signal.removeEventListener("abort", cancelled);
            if (signal.aborted) {
                cancelled();
                return;
            }
            active++;
            void task()
                .then(resolve, reject)
                .finally(() => {
                    active--;
                    drain();
                });
        };
        if (signal.aborted) {
            cancelled();
            return;
        }
        if (queue.length >= THUMBNAIL_PENDING) {
            reject(
                new DOMException(
                    "Thumbnail queue is busy",
                    "QuotaExceededError",
                ),
            );
            return;
        }
        signal.addEventListener("abort", cancelled, { once: true });
        queue.push(start);
        drain();
    });
}
export async function fetchThumbnail(
    url: string,
    signal: AbortSignal,
): Promise<string | null> {
    const first = await fetchThumbnailOnce(url, signal);
    if (typeof first !== "number") return first;
    // Wait outside the request queue so other visible files can load.
    await new Promise<void>((resolve, reject) => {
        signal.throwIfAborted();
        const cancel = () => {
            clearTimeout(timer);
            reject(signal.reason);
        };
        const timer = setTimeout(() => {
            signal.removeEventListener("abort", cancel);
            resolve();
        }, first);
        signal.addEventListener("abort", cancel, { once: true });
    });
    const retry = await fetchThumbnailOnce(url, signal);
    return typeof retry === "number" ? null : retry;
}

async function fetchThumbnailOnce(
    url: string,
    signal: AbortSignal,
): Promise<string | number | null> {
    return limited(signal, async () => {
        const response = await fetch(url, {
            credentials: "same-origin",
            headers: { Accept: "image/jpeg, image/png, image/webp" },
            signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
            // Each mount revalidates permission; HTTP ETags can reuse the small body.
            cache: "no-cache",
        });
        if (response.status === 503) {
            await response.body?.cancel();
            const retryAfter = Number(response.headers.get("retry-after") ?? 2);
            return Number.isFinite(retryAfter) &&
                retryAfter >= 0 &&
                retryAfter <= 5
                ? retryAfter * 1000
                : null;
        }
        const mime = response.headers.get("content-type")?.split(";")[0].trim();
        if (
            !response.ok ||
            !["image/jpeg", "image/png", "image/webp"].includes(mime || "")
        ) {
            await response.body?.cancel();
            return null;
        }
        if (
            Number(response.headers.get("content-length") || 0) >
            MAX_THUMBNAIL_BYTES
        ) {
            await response.body?.cancel();
            return null;
        }
        const reader = response.body?.getReader();
        if (!reader) return null;
        let length = 0;
        const parts: Uint8Array[] = [];
        try {
            while (true) {
                const chunk = await reader.read();
                if (chunk.done) break;
                length += chunk.value.byteLength;
                if (length > MAX_THUMBNAIL_BYTES) {
                    await reader.cancel();
                    return null;
                }
                parts.push(chunk.value);
            }
        } finally {
            reader.releaseLock();
        }
        if (!length || signal.aborted) return null;
        // The existing image CSP permits data URLs. Never expand it for grid previews.
        let binary = "";
        for (const part of parts)
            for (let i = 0; i < part.length; i += 8192)
                binary += String.fromCharCode(...part.subarray(i, i + 8192));
        return `data:${mime};base64,${btoa(binary)}`;
    });
}
