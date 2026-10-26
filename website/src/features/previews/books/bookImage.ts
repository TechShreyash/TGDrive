import { readBoundedResponse } from "../previewIO";

export const MAX_BOOK_IMAGE = 64 * 1024 * 1024;
const rasterTypes = new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
]);
const transient = new Set([408, 429, 500, 502, 503, 504]);
export type ImageProgress = {
    received: number;
    total: number;
    attempt: number;
    retrying: boolean;
};
class ImageResponseError extends Error {
    constructor(
        message: string,
        readonly retryable: boolean,
        readonly delay = 0,
    ) {
        super(message);
    }
}
function pause(ms: number, signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
        signal.throwIfAborted();
        const abort = () => {
            clearTimeout(timer);
            reject(signal.reason);
        };
        const timer = setTimeout(() => {
            signal.removeEventListener("abort", abort);
            resolve();
        }, ms);
        signal.addEventListener("abort", abort, { once: true });
    });
}
/** Original raster bytes only: no image conversion, resizing or base64. */
export async function loadBookImage(
    url: string,
    signal: AbortSignal,
    progress: (value: ImageProgress) => void,
): Promise<Blob> {
    for (let attempt = 1; ; attempt++) {
        let received = 0;
        let total = 0;
        progress({ received, total, attempt, retrying: false });
        try {
            const active = AbortSignal.any([
                signal,
                AbortSignal.timeout(90000),
            ]);
            const response = await fetch(url, {
                credentials: "same-origin",
                signal: active,
            });
            if (!response.ok) {
                let message =
                    "The page could not be loaded (" + response.status + ").";
                try {
                    const data = JSON.parse(
                        new TextDecoder().decode(
                            await readBoundedResponse(response, 65536, active),
                        ),
                    );
                    if (typeof data.detail === "string")
                        message = data.detail.slice(0, 500);
                } catch {
                    /* Keep the HTTP status if the error body is unreadable. */
                }
                const retry = response.headers.get("Retry-After") || "";
                const delay = /^\d+$/.test(retry)
                    ? Number(retry) * 1000
                    : Math.max(0, Date.parse(retry) - Date.now());
                throw new ImageResponseError(
                    message,
                    transient.has(response.status),
                    Number.isFinite(delay) ? delay : 0,
                );
            }
            const mime =
                response.headers.get("Content-Type")?.split(";")[0].trim() ||
                "";
            const encoding = response.headers
                .get("Content-Encoding")
                ?.trim()
                .toLowerCase();
            // Fetch decodes the body, but Content-Length counts encoded bytes.
            total =
                !encoding || encoding === "identity"
                    ? Number(response.headers.get("Content-Length")) || 0
                    : 0;
            if (!rasterTypes.has(mime) || total > MAX_BOOK_IMAGE) {
                await response.body?.cancel();
                throw new ImageResponseError(
                    "This page exceeds the 64 MiB image limit or is not a supported raster image.",
                    false,
                );
            }
            const reader = response.body?.getReader();
            if (!reader) throw new TypeError("The page response was empty.");
            const chunks: BlobPart[] = [];
            let complete = false;
            let updated = 0;
            const abort = () => {
                void reader.cancel().catch(() => {});
            };
            active.addEventListener("abort", abort, { once: true });
            try {
                while (true) {
                    active.throwIfAborted();
                    const { value, done } = await reader.read();
                    active.throwIfAborted();
                    if (done) {
                        complete = true;
                        break;
                    }
                    received += value.byteLength;
                    if (received > MAX_BOOK_IMAGE)
                        throw new ImageResponseError(
                            "This page exceeds the 64 MiB image limit.",
                            false,
                        );
                    chunks.push(value as Uint8Array<ArrayBuffer>);
                    if (Date.now() - updated > 200) {
                        progress({ received, total, attempt, retrying: false });
                        updated = Date.now();
                    }
                }
                if (total && received !== total)
                    throw new TypeError("The page download was interrupted.");
                progress({ received, total, attempt, retrying: false });
                return new Blob(chunks, { type: mime });
            } finally {
                active.removeEventListener("abort", abort);
                if (!complete) await reader.cancel().catch(() => {});
                reader.releaseLock();
            }
        } catch (cause) {
            signal.throwIfAborted();
            if (
                attempt >= 3 ||
                (cause instanceof ImageResponseError && !cause.retryable)
            )
                throw cause;
            progress({ received, total, attempt, retrying: true });
            await pause(
                Math.min(
                    30000,
                    Math.max(
                        2000 * attempt,
                        cause instanceof ImageResponseError ? cause.delay : 0,
                    ),
                ),
                signal,
            );
        }
    }
}
