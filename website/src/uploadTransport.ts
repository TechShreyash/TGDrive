export type UploadChunkTransport = (
  url: string,
  options: RequestInit & { body: Blob; signal: AbortSignal },
  progress: (bytes: number) => void,
) => Promise<Response>;

/** Upload events measure bytes sent by this browser, not durable server receipts. */
export const sendUploadChunk: UploadChunkTransport = (url, options, progress) =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const signal = options.signal;
    const cleanup = () => signal.removeEventListener("abort", abort);
    const abort = () => {
      xhr.abort();
      cleanup();
      reject(new DOMException("Upload paused", "AbortError"));
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
      if (!signal.aborted && Number.isFinite(event.loaded))
        progress(Math.max(0, Math.min(event.loaded, options.body.size)));
    };
    xhr.onload = () => {
      cleanup();
      const headers = new Headers();
      for (const line of xhr
        .getAllResponseHeaders()
        .trim()
        .split(/[\r\n]+/)) {
        const colon = line.indexOf(":");
        if (colon > 0)
          headers.append(line.slice(0, colon), line.slice(colon + 1).trim());
      }
      resolve(
        new Response(xhr.status === 204 ? null : xhr.responseText, {
          status: xhr.status,
          statusText: xhr.statusText,
          headers,
        }),
      );
    };
    xhr.onerror = () => {
      cleanup();
      reject(
        new TypeError("Sending this file was interrupted. Retry to resume."),
      );
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Upload paused", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
    try {
      xhr.send(options.body);
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
