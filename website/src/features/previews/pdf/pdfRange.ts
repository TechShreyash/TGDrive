import { readBoundedResponse } from "../previewIO";

// PDF.js ChunkedStream allocates new Uint8Array(length), even in range mode.
// Bound its source buffer as well as fetched data and visible-page rendering.
export const MAX_PDF_SOURCE_BYTES = 256 * 1024 ** 2;
// Bound buffers held by active requests, not all bytes read during an opening.
// PDF.js itself retains its source buffer; a cumulative cap prevents valid large
// documents from opening even when very few HTTP bytes have arrived.
export const PDF_RANGE_BUFFER_BUDGET_BYTES = 64 * 1024 ** 2;
export const PDF_RANGE_CHUNK_BYTES = 64 * 1024;
const HTTP_RANGE_BYTES = 1024 ** 2;
// PDF.js can fan out page-tree metadata reads before any pages render.
// Queue descriptors are tiny; the separate byte budget still bounds payloads.
const MAX_QUEUED_RANGES = 256;

export function checkPdfSize(size: number): void {
    if (!Number.isSafeInteger(size) || size <= 0)
        throw new Error(
            "This PDF has an invalid size. Download the original instead.",
        );
    if (size > MAX_PDF_SOURCE_BYTES)
        throw new Error(
            "PDF preview supports files up to 256 MiB to protect browser memory. Download the original to read larger files.",
        );
}

type Pending = {
    begin: number;
    end: number;
    resolve: (bytes: Uint8Array) => void;
    reject: (error: unknown) => void;
};

/** Only explicit, verified byte ranges; never fall back to a full-file GET. */
export class PdfRangeReader {
    private controller = new AbortController();
    private queue: Pending[] = [];
    private active = 0;
    private reserved = 0;
    private stopped = false;
    private etag: string | null | undefined;
    private received = 0;

    constructor(
        private url: string,
        private size: number,
        private onProgress?: (received: number) => void,
    ) {
        checkPdfSize(size);
    }

    read(begin: number, end: number): Promise<Uint8Array> {
        if (this.stopped) return Promise.reject(this.controller.signal.reason);
        if (
            !Number.isSafeInteger(begin) ||
            !Number.isSafeInteger(end) ||
            begin < 0 ||
            end <= begin ||
            end > this.size
        )
            return Promise.reject(
                new Error("The PDF requested an invalid byte range."),
            );
        if (this.queue.length >= MAX_QUEUED_RANGES)
            return Promise.reject(
                new Error(
                    "This PDF requires too many simultaneous reads. Download the original instead.",
                ),
            );
        return new Promise((resolve, reject) => {
            this.queue.push({ begin, end, resolve, reject });
            this.pump();
        });
    }

    close(): void {
        if (this.stopped) return;
        this.stopped = true;
        this.controller.abort();
        for (const job of this.queue.splice(0))
            job.reject(this.controller.signal.reason);
    }

    private pump(): void {
        while (!this.stopped && this.active < 2 && this.queue.length) {
            const job = this.queue[0];
            const length = job.end - job.begin;
            // One PDF.js metadata request may span most of a valid document. Run
            // that request alone rather than rejecting it or allocating peers too.
            if (
                this.active > 0 &&
                this.reserved + length > PDF_RANGE_BUFFER_BUDGET_BYTES
            )
                break;
            this.queue.shift();
            this.active += 1;
            this.reserved += length;
            void this.fetchRange(job.begin, job.end)
                .then(job.resolve, job.reject)
                .finally(() => {
                    this.active -= 1;
                    this.reserved -= length;
                    this.pump();
                });
        }
    }

    private async fetchRange(begin: number, end: number): Promise<Uint8Array> {
        const bytes = new Uint8Array(end - begin);
        for (let start = begin; start < end; start += HTTP_RANGE_BYTES) {
            const stop = Math.min(end, start + HTTP_RANGE_BYTES);
            const signal = AbortSignal.any([
                this.controller.signal,
                AbortSignal.timeout(60_000),
            ]);
            const response = await fetch(this.url, {
                credentials: "same-origin",
                cache: "no-store",
                headers: { Range: `bytes=${start}-${stop - 1}` },
                signal,
            });
            if (response.status !== 206) {
                await response.body?.cancel();
                if ([401, 403, 404, 410].includes(response.status))
                    throw new Error(
                        "This PDF is no longer available or access has expired. Close the preview and check the share link or sign in again.",
                    );
                throw new Error(
                    "PDF preview needs byte-range responses. Reload the preview or download the original; the server did not return the requested section.",
                );
            }
            if (
                response.headers.get("Content-Range") !==
                `bytes ${start}-${stop - 1}/${this.size}`
            ) {
                await response.body?.cancel();
                throw new Error(
                    "The PDF size or byte range changed. Close and reopen the preview.",
                );
            }
            const etag = response.headers.get("ETag");
            if (this.etag === undefined) this.etag = etag;
            else if (etag !== this.etag) {
                await response.body?.cancel();
                throw new Error(
                    "This PDF changed while reading. Close and reopen the preview.",
                );
            }
            const part = await readBoundedResponse(
                response,
                stop - start,
                signal,
            );
            signal.throwIfAborted();
            if (part.byteLength !== stop - start)
                throw new Error(
                    "A PDF section arrived incomplete. Reload the preview or download the original.",
                );
            bytes.set(part, start - begin);
            this.received += part.byteLength;
            this.onProgress?.(this.received);
        }
        return bytes;
    }
}
