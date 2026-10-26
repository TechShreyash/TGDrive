import { afterEach, describe, expect, it, vi } from "vitest";
import {
    checkPdfSize,
    PDF_RANGE_BUFFER_BUDGET_BYTES,
    MAX_PDF_SOURCE_BYTES,
    PdfRangeReader,
} from "./pdfRange";
afterEach(() => vi.unstubAllGlobals());

function ranged(size = 4096) {
    return vi.fn(async (_url: string, options: RequestInit) => {
        const range = (options.headers as Record<string, string>).Range;
        const [, begin, end] = /bytes=(\d+)-(\d+)/.exec(range)!;
        return new Response(
            new Uint8Array(Number(end) - Number(begin) + 1).fill(7),
            {
                status: 206,
                headers: { "Content-Range": `bytes ${begin}-${end}/${size}` },
            },
        );
    });
}

describe("bounded PDF byte ranges", () => {
    it("uses authenticated no-store ranges without a whole-file fallback", async () => {
        const fetcher = ranged();
        vi.stubGlobal("fetch", fetcher);
        const reader = new PdfRangeReader("/file?auth=share", 4096);
        expect(await reader.read(17, 49)).toEqual(new Uint8Array(32).fill(7));
        expect(fetcher.mock.calls[0]).toMatchObject([
            "/file?auth=share",
            {
                credentials: "same-origin",
                cache: "no-store",
                headers: { Range: "bytes=17-48" },
            },
        ]);
        reader.close();
    });
    it.each([200, 401, 403, 404, 410, 503])(
        "rejects status %s and cancels its body",
        async (status) => {
            const cancel = vi.fn();
            vi.stubGlobal(
                "fetch",
                vi.fn(
                    async () =>
                        new Response(new ReadableStream({ cancel }), {
                            status,
                        }),
                ),
            );
            const reader = new PdfRangeReader("/file", 4096);
            await expect(reader.read(0, 32)).rejects.toThrow(
                status === 200 || status === 503
                    ? "byte-range"
                    : "access has expired",
            );
            expect(cancel).toHaveBeenCalledOnce();
            reader.close();
        },
    );
    it("rejects changed source identity/size and short bodies", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(new Uint8Array(32), {
                        status: 206,
                        headers: { "Content-Range": "bytes 0-31/5000" },
                    }),
            ),
        );
        const reader = new PdfRangeReader("/file", 4096);
        await expect(reader.read(0, 32)).rejects.toThrow(
            "size or byte range changed",
        );
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(new Uint8Array(4), {
                        status: 206,
                        headers: { "Content-Range": "bytes 0-31/4096" },
                    }),
            ),
        );
        await expect(reader.read(0, 32)).rejects.toThrow("incomplete");
        reader.close();
    });
    it("rejects an equal-sized replacement document using the range ETag", async () => {
        let identity = "first";
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(new Uint8Array(32), {
                        status: 206,
                        headers: {
                            "Content-Range": "bytes 0-31/4096",
                            ETag: identity,
                        },
                    }),
            ),
        );
        const reader = new PdfRangeReader("/file", 4096);
        await reader.read(0, 32);
        identity = "replacement";
        await expect(reader.read(0, 32)).rejects.toThrow(
            "changed while reading",
        );
        reader.close();
    });
    it("splits large worker requests into at most 1 MiB HTTP ranges", async () => {
        const size = 3 * 1024 ** 2;
        const fetcher = ranged(size);
        vi.stubGlobal("fetch", fetcher);
        const reader = new PdfRangeReader("/file", size);
        expect((await reader.read(0, size)).byteLength).toBe(size);
        expect(fetcher).toHaveBeenCalledTimes(3);
        reader.close();
    });
    it("caps simultaneous reads and rejects both active and queued work on close", async () => {
        const signals: AbortSignal[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(
                (_url, options) =>
                    new Promise((_resolve, reject) => {
                        const signal = options.signal as AbortSignal;
                        signals.push(signal);
                        signal.addEventListener(
                            "abort",
                            () => reject(signal.reason),
                            {
                                once: true,
                            },
                        );
                    }),
            ),
        );
        const reader = new PdfRangeReader("/file", 4096);
        const pending = [
            reader.read(0, 10),
            reader.read(10, 20),
            reader.read(20, 30),
        ];
        const results = Promise.allSettled(pending);
        expect(signals).toHaveLength(2);
        reader.close();
        expect(
            (await results).every((result) => result.status === "rejected"),
        ).toBe(true);
        expect(signals.every((signal) => signal.aborted)).toBe(true);
        await expect(reader.read(30, 40)).rejects.toThrow();
    });
    it("keeps reading after 64 MiB of cumulative traffic", async () => {
        const size = 2 * 1024 ** 2;
        const fetcher = ranged(size);
        vi.stubGlobal("fetch", fetcher);
        const reader = new PdfRangeReader("/file", size);
        for (let index = 0; index < 66; index++)
            expect((await reader.read(0, 1024 ** 2)).byteLength).toBe(
                1024 ** 2,
            );
        expect(fetcher).toHaveBeenCalledTimes(66);
        reader.close();
    });
    it.each([40 * 1024 ** 2, PDF_RANGE_BUFFER_BUDGET_BYTES + 1])(
        "queues peers while a %s-byte active buffer owns the budget",
        async (length) => {
            const signals: AbortSignal[] = [];
            vi.stubGlobal(
                "fetch",
                vi.fn(
                    (_url, options) =>
                        new Promise((_resolve, reject) => {
                            const signal = options.signal as AbortSignal;
                            signals.push(signal);
                            signal.addEventListener(
                                "abort",
                                () => reject(signal.reason),
                                {
                                    once: true,
                                },
                            );
                        }),
                ),
            );
            const reader = new PdfRangeReader("/file", MAX_PDF_SOURCE_BYTES);
            const pending = [
                reader.read(0, length),
                reader.read(0, 40 * 1024 ** 2),
            ];
            const results = Promise.allSettled(pending);
            expect(signals).toHaveLength(1);
            reader.close();
            expect(
                (await results).every((result) => result.status === "rejected"),
            ).toBe(true);
        },
    );
    it("releases active buffer claims after failures so queued requests proceed", async () => {
        const length = 40 * 1024 ** 2;
        const fetcher = vi.fn(async () => new Response(null, { status: 503 }));
        vi.stubGlobal("fetch", fetcher);
        const reader = new PdfRangeReader("/file", MAX_PDF_SOURCE_BYTES);
        const results = await Promise.allSettled([
            reader.read(0, length),
            reader.read(0, length),
        ]);
        expect(results.every((result) => result.status === "rejected")).toBe(
            true,
        );
        expect(fetcher).toHaveBeenCalledTimes(2);
        reader.close();
    });
    it("rejects invalid ranges and unbounded source sizes before fetch", async () => {
        const fetcher = vi.fn();
        vi.stubGlobal("fetch", fetcher);
        expect(() => checkPdfSize(MAX_PDF_SOURCE_BYTES + 1)).toThrow("256 MiB");
        expect(() => checkPdfSize(NaN)).toThrow("invalid size");
        const reader = new PdfRangeReader("/file", MAX_PDF_SOURCE_BYTES);
        await expect(reader.read(0, MAX_PDF_SOURCE_BYTES + 1)).rejects.toThrow(
            "invalid byte range",
        );
        await expect(reader.read(-1, 20)).rejects.toThrow("invalid byte range");
        expect(fetcher).not.toHaveBeenCalled();
        reader.close();
    });
});
