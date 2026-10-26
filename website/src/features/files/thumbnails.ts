import { itemPath, type DriveItem } from "../../shared/types";

export const MAX_THUMBNAIL_BYTES = 128 * 1024;
export const THUMBNAIL_CONCURRENCY = 2;
export const THUMBNAIL_PENDING = 32;
const candidates = new Set([
    "cbr",
    "docx",
    "pptx",
    "xlsx",
    "odt",
    "odp",
    "ods",
    "mp3",
    "flac",
    "m4a",
    "ogg",
    "opus",
    "wav",
    "aac",
    "epub",
    "cbz",
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
    return limited(signal, async () => {
        const response = await fetch(url, {
            credentials: "same-origin",
            headers: { Accept: "image/jpeg, image/png, image/webp" },
            signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
            // Each mount revalidates permission; HTTP ETags can reuse the small body.
            cache: "no-cache",
        });
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
