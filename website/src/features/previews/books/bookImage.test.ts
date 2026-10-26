import { afterEach, describe, expect, it, vi } from "vitest";
import { loadBookImage, MAX_BOOK_IMAGE } from "./bookImage";
afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
});
const image = (bytes: Uint8Array<ArrayBuffer>) =>
    new Response(bytes, {
        headers: {
            "Content-Type": "image/png",
            "Content-Length": String(bytes.length),
        },
    });
describe("original book page downloads", () => {
    it("keeps a page above the old 16 MiB limit byte-for-byte and reports measured progress", async () => {
        const bytes = new Uint8Array(17 * 1024 * 1024);
        bytes[0] = 137;
        bytes[bytes.length - 1] = 41;
        vi.stubGlobal("fetch", vi.fn().mockResolvedValue(image(bytes)));
        const progress = vi.fn();
        const result = await loadBookImage(
            "/api/v1/books?entry=page.png",
            new AbortController().signal,
            progress,
        );
        expect(result.size).toBe(bytes.length);
        expect(
            Buffer.from(await result.arrayBuffer()).equals(
                Buffer.from(bytes.buffer),
            ),
        ).toBe(true);
        expect(progress).toHaveBeenLastCalledWith({
            received: bytes.length,
            total: bytes.length,
            attempt: 1,
            retrying: false,
        });
    });
    it.each(["gzip", "br", " GZip ", "gzip, br"])(
        "accepts decoded %s responses without comparing the compressed length",
        async (encoding) => {
            const bytes = new Uint8Array([1, 2, 3]);
            const response = image(bytes);
            response.headers.set("Content-Encoding", encoding);
            response.headers.set("Content-Length", "2");
            const fetcher = vi.fn().mockResolvedValue(response);
            vi.stubGlobal("fetch", fetcher);
            const progress = vi.fn();
            const result = await loadBookImage(
                "/page",
                new AbortController().signal,
                progress,
            );
            expect(new Uint8Array(await result.arrayBuffer())).toEqual(bytes);
            expect(progress).toHaveBeenLastCalledWith({
                received: bytes.length,
                total: 0,
                attempt: 1,
                retrying: false,
            });
            expect(fetcher).toHaveBeenCalledOnce();
        },
    );
    it.each([undefined, "identity"])(
        "still rejects an incomplete response with %s encoding",
        async (encoding) => {
            vi.useFakeTimers();
            const fetcher = vi.fn().mockImplementation(() => {
                const response = image(new Uint8Array([1, 2, 3]));
                response.headers.set("Content-Length", "4");
                if (encoding) response.headers.set("Content-Encoding", encoding);
                return Promise.resolve(response);
            });
            vi.stubGlobal("fetch", fetcher);
            const result = expect(
                loadBookImage("/page", new AbortController().signal, vi.fn()),
            ).rejects.toThrow("The page download was interrupted.");
            await vi.advanceTimersByTimeAsync(6000);
            await result;
            expect(fetcher).toHaveBeenCalledTimes(3);
        },
    );
    it("cancels compressed responses above the decoded image limit without retrying", async () => {
        const cancel = vi.fn();
        const chunk = new Uint8Array(1024 * 1024);
        const response = new Response(
            new ReadableStream({
                pull(controller) {
                    controller.enqueue(chunk);
                },
                cancel,
            }),
            {
                headers: {
                    "Content-Type": "image/png",
                    "Content-Encoding": "gzip",
                    "Content-Length": "1",
                },
            },
        );
        const fetcher = vi.fn().mockResolvedValue(response);
        vi.stubGlobal("fetch", fetcher);
        await expect(
            loadBookImage("/page", new AbortController().signal, vi.fn()),
        ).rejects.toThrow("64 MiB");
        expect(cancel).toHaveBeenCalledOnce();
        expect(fetcher).toHaveBeenCalledOnce();
    });
    it("shows permanent server errors without retrying", async () => {
        const fetcher = vi.fn().mockResolvedValue(
            new Response(
                JSON.stringify({
                    detail: "This resource exceeds the 64 MiB preview limit.",
                }),
                { status: 422 },
            ),
        );
        vi.stubGlobal("fetch", fetcher);
        await expect(
            loadBookImage("/page", new AbortController().signal, vi.fn()),
        ).rejects.toThrow("64 MiB");
        expect(fetcher).toHaveBeenCalledTimes(1);
    });
    it("automatically retries transient failures and honors Retry-After", async () => {
        vi.useFakeTimers();
        const fetcher = vi
            .fn()
            .mockResolvedValueOnce(
                new Response("{}", {
                    status: 503,
                    headers: { "Retry-After": "3" },
                }),
            )
            .mockResolvedValueOnce(image(new Uint8Array([1, 2, 3])));
        vi.stubGlobal("fetch", fetcher);
        const promise = loadBookImage(
            "/page",
            new AbortController().signal,
            vi.fn(),
        );
        await vi.advanceTimersByTimeAsync(2999);
        expect(fetcher).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect((await promise).size).toBe(3);
        expect(fetcher).toHaveBeenCalledTimes(2);
    });
    it("cancels retry work when the reader navigates away", async () => {
        vi.useFakeTimers();
        const fetcher = vi
            .fn()
            .mockRejectedValue(new TypeError("Network error"));
        vi.stubGlobal("fetch", fetcher);
        const controller = new AbortController();
        const result = expect(
            loadBookImage("/page", controller.signal, vi.fn()),
        ).rejects.toThrow();
        await vi.advanceTimersByTimeAsync(1);
        controller.abort();
        await result;
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(1);
    });
    it("rejects active content and oversized originals before buffering", async () => {
        const cancel = vi.fn();
        vi.stubGlobal(
            "fetch",
            vi.fn().mockResolvedValue({
                ok: true,
                headers: new Headers({ "Content-Type": "image/svg+xml" }),
                body: { cancel },
            }),
        );
        await expect(
            loadBookImage("/page", new AbortController().signal, vi.fn()),
        ).rejects.toThrow("supported raster");
        expect(cancel).toHaveBeenCalledTimes(1);
        vi.stubGlobal(
            "fetch",
            vi.fn().mockResolvedValue(
                new Response("", {
                    headers: {
                        "Content-Type": "image/png",
                        "Content-Length": String(MAX_BOOK_IMAGE + 1),
                    },
                }),
            ),
        );
        await expect(
            loadBookImage("/page", new AbortController().signal, vi.fn()),
        ).rejects.toThrow("64 MiB");
    });
});
