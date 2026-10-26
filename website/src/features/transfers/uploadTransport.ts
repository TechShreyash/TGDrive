export type UploadChunkTransport = (
    url: string,
    options: RequestInit & { body: Blob; signal: AbortSignal },
    progress: (bytes: number) => void,
) => Promise<Response>;

const UPLOAD_INACTIVITY_TIMEOUT_MS = 60000;

/** Upload events measure bytes sent by this browser, not durable server receipts. */
export const sendUploadChunk: UploadChunkTransport = (url, options, progress) =>
    new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const signal = options.signal;
        let settled = false;
        let sentBytes = 0;
        let inactivityTimer: ReturnType<typeof setTimeout> | undefined;
        const cleanup = () => {
            clearTimeout(inactivityTimer);
            signal.removeEventListener("abort", abort);
            xhr.upload.onprogress = null;
            xhr.onload = null;
            xhr.onerror = null;
            xhr.onabort = null;
        };
        const fail = (error: unknown, cancelRequest = false) => {
            if (settled) return;
            settled = true;
            cleanup();
            if (cancelRequest) xhr.abort();
            reject(error);
        };
        const abort = () => {
            fail(new DOMException("Upload paused", "AbortError"), true);
        };
        const resetInactivityTimer = () => {
            clearTimeout(inactivityTimer);
            inactivityTimer = setTimeout(() => {
                fail(
                    new DOMException(
                        "TGDrive stopped acknowledging this upload. Checking its position before retrying.",
                        "TimeoutError",
                    ),
                    true,
                );
            }, UPLOAD_INACTIVITY_TIMEOUT_MS);
        };
        if (signal.aborted) {
            abort();
            return;
        }
        xhr.open("PATCH", url);
        xhr.withCredentials = true;
        new Headers(options.headers).forEach((value, name) =>
            xhr.setRequestHeader(name, value),
        );
        xhr.upload.onprogress = (event) => {
            if (settled || signal.aborted || !Number.isFinite(event.loaded))
                return;
            const sent = Math.max(0, Math.min(event.loaded, options.body.size));
            if (sent > sentBytes) {
                sentBytes = sent;
                resetInactivityTimer();
            }
            progress(sent);
        };
        xhr.onload = () => {
            if (settled) return;
            try {
                const headers = new Headers();
                for (const line of xhr
                    .getAllResponseHeaders()
                    .trim()
                    .split(/[\r\n]+/)) {
                    const colon = line.indexOf(":");
                    if (colon > 0)
                        headers.append(
                            line.slice(0, colon),
                            line.slice(colon + 1).trim(),
                        );
                }
                const response = new Response(
                    xhr.status === 204 ? null : xhr.responseText,
                    {
                        status: xhr.status,
                        statusText: xhr.statusText,
                        headers,
                    },
                );
                settled = true;
                cleanup();
                resolve(response);
            } catch (error) {
                fail(error);
            }
        };
        xhr.onerror = () => {
            fail(
                new TypeError(
                    "Sending this file was interrupted. Retry to resume.",
                ),
            );
        };
        xhr.onabort = () => {
            fail(new DOMException("Upload paused", "AbortError"));
        };
        signal.addEventListener("abort", abort, { once: true });
        try {
            // A slow chunk can keep sending for minutes. Only a lack of new payload
            // bytes (including the wait for the final server receipt) times out.
            resetInactivityTimer();
            xhr.send(options.body);
        } catch (error) {
            fail(error);
        }
    });
