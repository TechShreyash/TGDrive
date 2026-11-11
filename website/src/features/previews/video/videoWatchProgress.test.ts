import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    formatVideoTime,
    readVideoPosition,
    saveVideoPosition,
    loadServerVideoPosition,
    createVideoProgressWriter,
    readLocalVideoPositions,
    loadServerVideoPositions,
    savedVideoResumeTime,
    VIDEO_POSITION_CHANGED,
    videoPositionChange,
} from "./videoWatchProgress";

const prefix = "tgdrive.video.position.v1:";
let stored: Map<string, string>;

beforeEach(() => {
    stored = new Map();
    vi.stubGlobal("localStorage", {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
        removeItem: (key: string) => stored.delete(key),
    });
    vi.stubGlobal("window", new EventTarget());
});
afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("video position labels", () => {
    it.each<[number, string]>([
        [0, "0:00"],
        [37.9, "0:37"],
        [60, "1:00"],
        [3599.9, "59:59"],
        [3600, "1:00:00"],
        [3657, "1:00:57"],
        [4320, "1:12:00"],
        [4635, "1:17:15"],
        [36000, "10:00:00"],
        [86400, "24:00:00"],
    ])("formats %s seconds as %s", (seconds, label) => {
        expect(formatVideoTime(seconds)).toBe(label);
    });
});

describe("default saved resume boundaries", () => {
    it.each([
        [0, 5, 0],
        [2, 5, 0],
        [2.01, 5, 2.01],
        [2.99, 5, 2.99],
        [3, 5, 0],
        [1, 2, 0],
        [180, 3600, 180],
        [3420, 3600, 3420],
        [230, 240, 230],
        [3597.9, 3600, 3597.9],
        [3598, 3600, 0],
        [4000, 3600, 0],
        [Number.NaN, 10, 0],
        [5, Number.POSITIVE_INFINITY, 0],
    ])("uses saved time %s of %s seconds as %s", (time, duration, expected) => {
        expect(savedVideoResumeTime(time, duration)).toBe(expected);
    });
});

describe("local video watch positions", () => {
    it("keeps files independent and clears a finished or restarted video", () => {
        saveVideoPosition("first-file", 37.5, "share");
        saveVideoPosition("second-file", 9, "share");
        expect(readVideoPosition("first-file", "share")).toBe(37.5);
        expect(readVideoPosition("second-file", "share")).toBe(9);
        expect(readVideoPosition("unwatched-file", "share")).toBeNull();
        expect(stored.get("tgdrive.video.position.v2:share:first-file")).toBe(
            "37.5",
        );
        saveVideoPosition("first-file", 0, "share");
        expect(readVideoPosition("first-file", "share")).toBeNull();
        expect(readVideoPosition("second-file", "share")).toBe(9);
    });

    it.each(["", "garbage", "{}", "null", "-1", "0", "NaN", "Infinity"])(
        "ignores an invalid stored position: %s",
        (value) => {
            stored.set(prefix + "file", value);
            expect(readVideoPosition("file", "share")).toBeNull();
        },
    );

    it.each([-1, Number.NaN, Number.POSITIVE_INFINITY])(
        "preserves a good bookmark when the engine reports %s",
        (value) => {
            saveVideoPosition("file", 37.5, "share");
            saveVideoPosition("file", value, "share");
            expect(readVideoPosition("file", "share")).toBe(37.5);
        },
    );

    it("allows playback when storage access, writes or removal fail", () => {
        vi.stubGlobal("localStorage", {
            getItem() {
                throw new Error("Storage blocked");
            },
            setItem() {
                throw new Error("Storage full");
            },
            removeItem() {
                throw new Error("Storage blocked");
            },
        });
        expect(readVideoPosition("file", "share")).toBeNull();
        expect(() => saveVideoPosition("file", 37.5, "share")).not.toThrow();
        expect(() => saveVideoPosition("file", 0, "share")).not.toThrow();
    });
    it("isolates share/demo scope and does not resurrect legacy progress after clearing", () => {
        stored.set(prefix + "file", "4");
        expect(readVideoPosition("file", "share")).toBe(4);
        saveVideoPosition("file", 10, "share");
        saveVideoPosition("file", 8, "demo");
        expect(readVideoPosition("file", "share")).toBe(10);
        expect(readVideoPosition("file", "demo")).toBe(8);
        saveVideoPosition("file", 0, "share");
        expect(readVideoPosition("file", "share")).toBeNull();
        expect(stored.get(prefix + "file")).toBe("4");
    });
});

describe("bounded thumbnail video progress", () => {
    it("publishes validated ordinary and final acknowledgements, without writer fields", async () => {
        vi.useFakeTimers();
        const events: Event[] = [];
        window.addEventListener(VIDEO_POSITION_CHANGED, (event) =>
            events.push(event),
        );
        vi.stubGlobal(
            "fetch",
            vi.fn(async (_url: string, options: RequestInit) => {
                if (options.method === "POST")
                    return Response.json({
                        session_id: "a".repeat(32),
                        save_interval_seconds: 5,
                        progress: null,
                    });
                const body = JSON.parse(options.body as string);
                return Response.json({
                    progress: { ...body, updated_at: 100 },
                });
            }),
        );
        const writer = createVideoProgressWriter("file", vi.fn());
        writer.start();
        writer.save(30, 120);
        await vi.advanceTimersByTimeAsync(0);
        writer.save(40, 120);
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
        expect(events.map(videoPositionChange)).toEqual([
            {
                fileId: "file",
                mode: "server",
                position: {
                    position_seconds: 30,
                    duration_seconds: 120,
                    updated_at: 100,
                },
            },
            {
                fileId: "file",
                mode: "server",
                position: {
                    position_seconds: 40,
                    duration_seconds: 120,
                    updated_at: 100,
                },
            },
        ]);
    });

    it("emits local summaries only after measured duration is stored successfully", () => {
        const events: Event[] = [];
        window.addEventListener(VIDEO_POSITION_CHANGED, (event) =>
            events.push(event),
        );
        saveVideoPosition("file", 30, "share");
        expect(events).toEqual([]);
        saveVideoPosition("file", 30, "share", 120);
        expect(videoPositionChange(events[0])).toEqual({
            fileId: "file",
            mode: "share",
            position: expect.objectContaining({
                position_seconds: 30,
                duration_seconds: 120,
            }),
        });
        vi.stubGlobal("localStorage", {
            setItem() {
                throw new Error("Blocked");
            },
        });
        saveVideoPosition("file", 50, "share", 120);
        expect(events).toHaveLength(1);
    });

    it.each([
        null,
        { fileId: "../file", mode: "server", position: null },
        { fileId: "file", mode: "owner", position: null },
        {
            fileId: "file",
            mode: "server",
            position: {
                position_seconds: 200,
                duration_seconds: 120,
                updated_at: 1,
            },
        },
        {
            fileId: "file",
            mode: "share",
            position: {
                position_seconds: 30,
                duration_seconds: 0,
                updated_at: 1,
            },
        },
    ])("rejects an unverified progress event %j", (detail) => {
        expect(
            videoPositionChange(
                new CustomEvent(VIDEO_POSITION_CHANGED, { detail }),
            ),
        ).toBeNull();
    });

    it("uses measured duration only, and never guesses from a legacy time", () => {
        saveVideoPosition("legacy", 40, "share");
        saveVideoPosition("known", 30, "share", 120);
        expect(readLocalVideoPositions(["legacy", "known"], "share")).toEqual({
            known: expect.objectContaining({
                position_seconds: 30,
                duration_seconds: 120,
            }),
        });
        expect(readLocalVideoPositions(["known"], "demo")).toEqual({});
        saveVideoPosition("known", 50, "share");
        expect(readVideoPosition("known", "share")).toBe(50);
        expect(readLocalVideoPositions(["known"], "share")).toEqual({});
    });

    it("bounds measured local summaries without deleting legacy resume positions", () => {
        for (let index = 0; index < 201; index++)
            saveVideoPosition("file-" + index, 30, "share", 120);
        expect(
            Object.keys(
                JSON.parse(stored.get("tgdrive.video.summaries.v1:share")!),
            ),
        ).toHaveLength(200);
        expect(
            readLocalVideoPositions(["file-0", "file-200"], "share"),
        ).toEqual({
            "file-200": expect.objectContaining({ position_seconds: 30 }),
        });
        expect(readVideoPosition("file-0", "share")).toBe(30);
    });

    it("requests one bounded owner batch and accepts only requested verified records", async () => {
        const fetcher = vi.fn(async (_url: string, _options: RequestInit) =>
            Response.json({
                progress: {
                    file: {
                        position_seconds: 30,
                        duration_seconds: 120,
                        updated_at: 100,
                    },
                },
            }),
        );
        vi.stubGlobal("fetch", fetcher);
        const rows = await loadServerVideoPositions(
            ["file", "file", "missing"],
            new AbortController().signal,
        );
        expect(rows.file.position_seconds / rows.file.duration_seconds).toBe(
            0.25,
        );
        expect(fetcher).toHaveBeenCalledTimes(1);
        expect(fetcher.mock.calls[0][0]).toBe(
            "/api/v1/video-progress/summaries",
        );
        expect(JSON.parse(fetcher.mock.calls[0][1].body as string)).toEqual({
            file_ids: ["file", "missing"],
        });
        fetcher.mockResolvedValueOnce(
            Response.json({
                progress: {
                    other: {
                        position_seconds: 30,
                        duration_seconds: 120,
                        updated_at: 100,
                    },
                },
            }),
        );
        await expect(
            loadServerVideoPositions(["file"], new AbortController().signal),
        ).rejects.toThrow("could not be verified");
    });

    it("rejects an oversized batch before transport", async () => {
        const fetcher = vi.fn();
        vi.stubGlobal("fetch", fetcher);
        await expect(
            loadServerVideoPositions(
                Array.from({ length: 129 }, (_, index) => "file-" + index),
                new AbortController().signal,
            ),
        ).rejects.toThrow("128 files");
        expect(fetcher).not.toHaveBeenCalled();
    });
});

describe("owner server video progress", () => {
    const session = {
        session_id: "a".repeat(32),
        progress: null,
        save_interval_seconds: 5,
    };
    function transport() {
        const writes: Record<string, unknown>[] = [];
        const fetcher = vi.fn(async (_url: string, options: RequestInit) => {
            if (options.method === "POST") return Response.json(session);
            const body = JSON.parse(options.body as string);
            writes.push(body);
            return Response.json({ progress: { ...body, updated_at: 1000 } });
        });
        vi.stubGlobal("fetch", fetcher);
        return { fetcher, writes };
    }
    it("loads only owner API data without importing device-local history or share authority", async () => {
        stored.set(prefix + "file", "37.5");
        const fetcher = vi.fn(async () => Response.json({ progress: null }));
        vi.stubGlobal("fetch", fetcher);
        expect(
            await loadServerVideoPosition("file", new AbortController().signal),
        ).toBeNull();
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(url).toBe("/api/v1/files/file/video-progress");
        expect(options.credentials).toBe("same-origin");
        expect(stored.size).toBe(1);
    });
    it("rejects malformed saved positions rather than fabricating zero", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    progress: {
                        position_seconds: 100,
                        duration_seconds: 50,
                        updated_at: 1,
                    },
                }),
            ),
        );
        await expect(
            loadServerVideoPosition("file", new AbortController().signal),
        ).rejects.toThrow("could not be verified");
    });
    it("keeps advisory progress authorization errors local to playback", async () => {
        const expired = vi.fn();
        window.addEventListener("tgdrive:auth-required", expired);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({ detail: "Session expired" }, { status: 401 }),
            ),
        );
        await expect(
            loadServerVideoPosition("file", new AbortController().signal),
        ).rejects.toMatchObject({ status: 401 });
        const problem = vi.fn();
        const writer = createVideoProgressWriter("file", problem);
        writer.start();
        await vi.waitFor(() => expect(problem).toHaveBeenCalledOnce());
        writer.close();
        expect(expired).not.toHaveBeenCalled();
    });
    it("begins on play only, coalesces regular saves and sends one ordered closing save", async () => {
        vi.useFakeTimers();
        const { fetcher, writes } = transport();
        const writer = createVideoProgressWriter("file", vi.fn());
        writer.save(1, 100);
        await vi.advanceTimersByTimeAsync(0);
        expect(fetcher).not.toHaveBeenCalled();
        writer.start();
        await vi.advanceTimersByTimeAsync(0);
        expect(writes).toHaveLength(1);
        writer.save(2, 100);
        writer.save(3, 100);
        await vi.advanceTimersByTimeAsync(4999);
        expect(writes).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(writes[1]).toMatchObject({
            position_seconds: 3,
            sequence: 2,
            final: false,
        });
        writer.save(4, 100);
        writer.close();
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
        expect(writes).toHaveLength(3);
        expect(writes[2]).toMatchObject({
            position_seconds: 4,
            sequence: 3,
            final: true,
        });
        writer.save(5, 100);
        await vi.advanceTimersByTimeAsync(10000);
        expect(writes).toHaveLength(3);
        expect(stored.size).toBe(0);
    });
    it("retains the owned closing save when session creation finishes after close", async () => {
        vi.useFakeTimers();
        let release!: (value: Response) => void;
        const writes: Record<string, unknown>[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (_url: string, options: RequestInit) => {
                if (options.method === "POST")
                    return new Promise<Response>((resolve) => {
                        release = resolve;
                    });
                const body = JSON.parse(options.body as string);
                writes.push(body);
                return Response.json({
                    progress: { ...body, updated_at: 1000 },
                });
            }),
        );
        const writer = createVideoProgressWriter("file", vi.fn());
        writer.start();
        writer.save(8, 100);
        writer.close();
        release(Response.json(session));
        await vi.advanceTimersByTimeAsync(0);
        expect(writes).toHaveLength(1);
        expect(writes[0]).toMatchObject({ position_seconds: 8, final: true });
    });
    it("retains the latest snapshot received while a regular save is pending", async () => {
        vi.useFakeTimers();
        let release!: (response: Response) => void;
        const { fetcher, writes } = transport();
        fetcher.mockResolvedValueOnce(Response.json(session));
        fetcher.mockImplementationOnce(
            (_url, options) =>
                new Promise<Response>((resolve) => {
                    writes.push(JSON.parse(options.body as string));
                    release = resolve;
                }),
        );
        const writer = createVideoProgressWriter("file", vi.fn());
        writer.start();
        writer.save(1, 100);
        await vi.advanceTimersByTimeAsync(0);
        writer.save(2, 100);
        writer.save(3, 100);
        release(
            Response.json({ progress: { ...writes[0], updated_at: 1000 } }),
        );
        await vi.advanceTimersByTimeAsync(4999);
        expect(writes).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(writes).toHaveLength(2);
        expect(writes[1]).toMatchObject({ position_seconds: 3, sequence: 2 });
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
    });
    it.each([200, 409])(
        "keeps closed writers quiet when a pending save returns %s",
        async (status) => {
            vi.useFakeTimers();
            let release!: (response: Response) => void;
            const { fetcher, writes } = transport();
            fetcher.mockResolvedValueOnce(Response.json(session));
            fetcher.mockImplementationOnce(
                (_url, options) =>
                    new Promise<Response>((resolve) => {
                        writes.push(JSON.parse(options.body as string));
                        release = resolve;
                    }),
            );
            const problem = vi.fn();
            const writer = createVideoProgressWriter("file", problem);
            writer.start();
            writer.save(1, 100);
            await vi.advanceTimersByTimeAsync(0);
            writer.save(3, 100);
            writer.close();
            release(
                Response.json(
                    status === 200
                        ? {
                              progress: {
                                  position_seconds: 1,
                                  duration_seconds: 100,
                                  updated_at: 1000,
                              },
                          }
                        : {
                              code: "VIDEO_PROGRESS_SESSION_CHANGED",
                              detail: "Newer writer",
                          },
                    { status },
                ),
            );
            await vi.advanceTimersByTimeAsync(10000);
            expect(problem).not.toHaveBeenCalled();
            expect(writes).toHaveLength(status === 200 ? 2 : 1);
            if (status === 200)
                expect(writes[1]).toMatchObject({
                    position_seconds: 3,
                    sequence: 2,
                    final: true,
                });
        },
    );
    it("does not reclaim a writer or publish a closing save after a conflict", async () => {
        vi.useFakeTimers();
        const problem = vi.fn();
        const fetcher = vi.fn(async (_url: string, options: RequestInit) =>
            options.method === "POST"
                ? Response.json(session)
                : Response.json(
                      {
                          code: "VIDEO_PROGRESS_SESSION_CHANGED",
                          detail: "Newer writer",
                      },
                      { status: 409 },
                  ),
        );
        vi.stubGlobal("fetch", fetcher);
        const writer = createVideoProgressWriter("file", problem);
        writer.start();
        writer.save(8, 100);
        await vi.advanceTimersByTimeAsync(0);
        expect(problem).toHaveBeenCalledWith(
            expect.stringContaining("Another playback session"),
        );
        writer.save(9, 100);
        await vi.advanceTimersByTimeAsync(10000);
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
        expect(fetcher).toHaveBeenCalledTimes(2);
    });
    it("retries only a confirmed begin rate rejection once and cancels it on close", async () => {
        vi.useFakeTimers();
        const fetcher = vi.fn(async () =>
            Response.json(
                { code: "VIDEO_PROGRESS_RATE_LIMITED", detail: "Wait" },
                { status: 429, headers: { "Retry-After": "1" } },
            ),
        );
        vi.stubGlobal("fetch", fetcher);
        const writer = createVideoProgressWriter("file", vi.fn());
        writer.start();
        await vi.advanceTimersByTimeAsync(0);
        writer.start();
        expect(fetcher).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1000);
        expect(fetcher).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(2);
        writer.close();
        const other = createVideoProgressWriter("other", vi.fn());
        other.start();
        await vi.advanceTimersByTimeAsync(0);
        other.close();
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(3);
    });
    it("paces regular rate rejections and clears the problem only after an acknowledged save", async () => {
        vi.useFakeTimers();
        let attempts = 0;
        const problem = vi.fn();
        vi.stubGlobal(
            "fetch",
            vi.fn(async (_url: string, options: RequestInit) => {
                if (options.method === "POST") return Response.json(session);
                if (++attempts === 1)
                    return Response.json(
                        { code: "VIDEO_PROGRESS_RATE_LIMITED", detail: "Wait" },
                        { status: 429, headers: { "Retry-After": "10" } },
                    );
                const body = JSON.parse(options.body as string);
                return Response.json({
                    progress: { ...body, updated_at: 1000 },
                });
            }),
        );
        const writer = createVideoProgressWriter("file", problem);
        writer.start();
        writer.save(1, 100);
        await vi.advanceTimersByTimeAsync(0);
        expect(attempts).toBe(1);
        writer.save(2, 100);
        await vi.advanceTimersByTimeAsync(9999);
        expect(attempts).toBe(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(attempts).toBe(2);
        expect(problem).toHaveBeenLastCalledWith("");
        writer.close();
        await vi.advanceTimersByTimeAsync(0);
    });
});
