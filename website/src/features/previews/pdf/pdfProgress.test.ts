import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    createPdfProgressWriter,
    loadServerPdfPosition,
    pdfResumePage,
    readLocalPdfPosition,
    saveLocalPdfPosition,
} from "./pdfProgress";

let stored: Map<string, string>;
beforeEach(() => {
    stored = new Map();
    vi.stubGlobal("localStorage", {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
    });
});
afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("device PDF page positions", () => {
    it("keeps scopes and files separate and requires the actual page count", () => {
        saveLocalPdfPosition("first", "share", 4, 10);
        saveLocalPdfPosition("first", "demo", 2, 10);
        saveLocalPdfPosition("second", "share", 8, 20);
        const saved = readLocalPdfPosition("first", "share");
        expect(pdfResumePage(saved, 10)).toBe(4);
        expect(pdfResumePage(saved, 9)).toBeNull();
        expect(readLocalPdfPosition("first", "demo")?.page_number).toBe(2);
        expect(readLocalPdfPosition("second", "share")?.page_number).toBe(8);
        expect(readLocalPdfPosition("missing", "share")).toBeNull();
    });

    it("retains at most 200 local records and refreshes a revisited entry", () => {
        for (let index = 0; index < 200; index++)
            saveLocalPdfPosition("file-" + index, "share", 2, 10);
        saveLocalPdfPosition("file-0", "share", 3, 10);
        saveLocalPdfPosition("new", "share", 5, 10);
        expect(readLocalPdfPosition("file-0", "share")?.page_number).toBe(3);
        expect(readLocalPdfPosition("file-1", "share")).toBeNull();
        expect(
            Object.keys(
                JSON.parse(stored.get("tgdrive.pdf.positions.v1:share")!),
            ),
        ).toHaveLength(200);
    });

    it.each([0, -1, 1.5, 11, Number.NaN, Number.POSITIVE_INFINITY])(
        "rejects invalid page %s without replacing a good record",
        (page) => {
            saveLocalPdfPosition("file", "share", 4, 10);
            expect(() =>
                saveLocalPdfPosition("file", "share", page, 10),
            ).toThrow();
            expect(readLocalPdfPosition("file", "share")?.page_number).toBe(4);
        },
    );

    it("ignores malformed/oversized storage but reports unavailable storage", () => {
        stored.set("tgdrive.pdf.positions.v1:share", "{");
        expect(readLocalPdfPosition("file", "share")).toBeNull();
        stored.set(
            "tgdrive.pdf.positions.v1:share",
            " ".repeat(128 * 1024 + 1),
        );
        expect(readLocalPdfPosition("file", "share")).toBeNull();
        vi.stubGlobal("localStorage", {
            getItem() {
                throw new Error("Storage blocked");
            },
        });
        expect(() => readLocalPdfPosition("file", "share")).toThrow(
            "Storage blocked",
        );
        expect(() => saveLocalPdfPosition("file", "share", 4, 10)).toThrow(
            "Storage blocked",
        );
    });
});

describe("owner PDF progress", () => {
    it.each([
        { page_number: 1.5, page_count: 12, updated_at: 1 },
        { page_number: true, page_count: 12, updated_at: 1 },
        { page_number: 1, page_count: 10001, updated_at: 1 },
        { page_number: 1, page_count: 12, updated_at: -1 },
    ])("rejects an invalid server bookmark %j", async (progress) => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ progress })),
        );
        await expect(
            loadServerPdfPosition("file", new AbortController().signal),
        ).rejects.toThrow("could not be verified");
    });

    it("reads only server state and rejects unverified page metadata", async () => {
        saveLocalPdfPosition("file", "share", 4, 10);
        const fetcher = vi.fn(async (_url: string, _options: RequestInit) =>
            Response.json({ progress: null }),
        );
        vi.stubGlobal("fetch", fetcher);
        expect(
            await loadServerPdfPosition("file", new AbortController().signal),
        ).toBeNull();
        expect(fetcher.mock.calls[0][0]).toBe(
            "/api/v1/files/file/pdf-progress",
        );
        fetcher.mockResolvedValueOnce(
            Response.json({
                progress: { page_number: 0, page_count: 10, updated_at: 100 },
            }),
        );
        await expect(
            loadServerPdfPosition("file", new AbortController().signal),
        ).rejects.toThrow("could not be verified");
    });

    it("coalesces changed pages and closes with the newest ordered final save", async () => {
        vi.useFakeTimers();
        const writes: Record<string, unknown>[] = [];
        const fetcher = vi.fn(async (_url: string, options: RequestInit) => {
            if (options.method === "POST")
                return Response.json({
                    session_id: "a".repeat(32),
                    save_interval_seconds: 5,
                    progress: null,
                });
            const body = JSON.parse(options.body as string);
            writes.push(body);
            return Response.json({
                progress: {
                    page_number: body.page_number,
                    page_count: body.page_count,
                    updated_at: 100,
                },
            });
        });
        vi.stubGlobal("fetch", fetcher);
        const writer = createPdfProgressWriter("file", vi.fn());
        writer.start();
        writer.save(2, 10);
        await vi.advanceTimersByTimeAsync(0);
        writer.save(3, 10);
        writer.save(4, 10);
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
        expect(
            writes.map(({ page_number, sequence, final }) => ({
                page_number,
                sequence,
                final,
            })),
        ).toEqual([
            { page_number: 2, sequence: 1, final: false },
            { page_number: 4, sequence: 2, final: true },
        ]);
        writer.save(5, 10);
        writer.start();
        await vi.advanceTimersByTimeAsync(10000);
        expect(writes).toHaveLength(2);
        expect(writer.closed).toBe(true);
    });
});
