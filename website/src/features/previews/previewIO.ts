export class PreviewLimitError extends Error {}
/** Read no more than limit bytes, releasing the request on every early exit. */
export async function readBoundedResponse(
    response: Response,
    limit: number,
    signal: AbortSignal,
): Promise<Uint8Array> {
    const declared = Number(response.headers.get("Content-Length"));
    if (Number.isFinite(declared) && declared > limit) {
        await response.body?.cancel();
        throw new PreviewLimitError("Preview exceeds the allowed size.");
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("The preview response was empty.");
    const abort = () => {
        void reader.cancel().catch(() => {});
    };
    signal.addEventListener("abort", abort, { once: true });
    const chunks: Uint8Array[] = [];
    let size = 0,
        complete = false;
    try {
        signal.throwIfAborted();
        while (true) {
            const { done, value } = await reader.read();
            signal.throwIfAborted();
            if (done) {
                complete = true;
                break;
            }
            size += value.byteLength;
            if (size > limit)
                throw new PreviewLimitError(
                    "Preview exceeds the allowed size.",
                );
            chunks.push(value);
        }
    } finally {
        signal.removeEventListener("abort", abort);
        if (!complete) await reader.cancel().catch(() => {});
        reader.releaseLock();
    }
    const data = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
        data.set(chunk, offset);
        offset += chunk.length;
    }
    return data;
}
