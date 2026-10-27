import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    BROWSER_RECEIVE_CONCURRENCY,
    BROWSER_RECEIVE_INITIAL_CONCURRENCY,
    TransferManager,
} from "./transfers";
import type { UploadChunkTransport } from "./uploadTransport";
import { stubDeviceLocks } from "./transferTestLocks";

function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
        resolve = done;
    });
    return { promise, resolve };
}

const receipt = (id: string, size = 4) =>
    Response.json({
        id,
        offset: 0,
        total_size: size,
        chunk_size: 4,
        checksums: [],
    });
const position = (size = 4) =>
    new Response(null, {
        headers: { "Upload-Offset": "0", "Upload-Length": String(size) },
    });
const received = (offset = 4) =>
    new Response(null, {
        status: 204,
        headers: { "Upload-Offset": String(offset) },
    });
const managers: TransferManager[] = [];
const managerForTest = (sendChunk?: UploadChunkTransport) => {
    const manager = new TransferManager(
        sendChunk || ((url, options) => fetch(url, options)),
    );
    managers.push(manager);
    return manager;
};

beforeEach(() => {
    stubDeviceLocks();
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
        get length() {
            return values.size;
        },
        key: (index: number) => [...values.keys()][index] ?? null,
        removeItem: (key: string) => values.delete(key),
        getItem: (key: string) => values.get(key) || null,
        setItem: (key: string, value: string) => values.set(key, value),
    });
    vi.stubGlobal("window", new EventTarget());
});

afterEach(() => {
    for (const manager of managers.splice(0)) manager.setEnabled(false);
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("device upload scheduling", () => {
    it.each(["create", "head", "chunk"])(
        "honors a server-wide Retry-After from %s without admitting more files early",
        async (stage) => {
            vi.useFakeTimers();
            const progress = deferred<Response>();
            let rejected = 0;
            let created = 0;
            vi.stubGlobal(
                "fetch",
                vi.fn(async (url: string, options: RequestInit = {}) => {
                    if (url === "/api/v1/jobs") return progress.promise;
                    const isCreate = url === "/api/v1/uploads";
                    if (isCreate) created++;
                    const shouldReject =
                        stage === "create"
                            ? isCreate
                            : stage === "head"
                              ? options.method === "HEAD"
                              : options.method === "PATCH";
                    if (
                        shouldReject &&
                        rejected < BROWSER_RECEIVE_INITIAL_CONCURRENCY
                    ) {
                        rejected++;
                        return Response.json(
                            { detail: "Slow down" },
                            {
                                status: 429,
                                headers: { "Retry-After": "30" },
                            },
                        );
                    }
                    if (isCreate) return receipt("rate-" + created);
                    if (options.method === "HEAD") return position();
                    if (options.method === "PATCH") return received();
                    const id = url.split("/")[4];
                    if (url.endsWith("/complete")) return Response.json({ id });
                    return receipt(id);
                }),
            );
            const manager = managerForTest();
            manager.add(
                Array.from(
                    { length: 5 },
                    (_, i) => new File(["data"], "file-" + i),
                ),
                "/",
                10,
            );
            if (stage === "head")
                for (const [index, item] of manager
                    .snapshot()
                    .slice(0, BROWSER_RECEIVE_INITIAL_CONCURRENCY)
                    .entries())
                    item.uploadId = "resumed-" + index;
            const initialCreations =
                stage === "head" ? 0 : BROWSER_RECEIVE_INITIAL_CONCURRENCY;
            try {
                manager.setEnabled(true);
                await vi.advanceTimersByTimeAsync(0);
                expect(rejected).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
                expect(created).toBe(initialCreations);
                await vi.advanceTimersByTimeAsync(29999);
                expect(created).toBe(initialCreations);
                await vi.advanceTimersByTimeAsync(1);
                expect(
                    manager.snapshot().every((item) => Boolean(item.jobId)),
                ).toBe(true);
            } finally {
                manager.setEnabled(false);
                progress.resolve(Response.json({ jobs: [] }));
                await vi.advanceTimersByTimeAsync(0);
            }
        },
    );
    it("keeps a cancellation final when an earlier receipt snapshot arrives late", async () => {
        const snapshot = deferred<Response>();
        const snapshotStarted = deferred<void>();
        let chunks = 0;
        let completions = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string, options: RequestInit = {}) => {
                if (url === "/api/v1/jobs") return Response.json({ jobs: [] });
                if (url === "/api/v1/uploads")
                    return receipt("cancelled-receipt");
                if (url.endsWith("/cancel"))
                    return Response.json({ state: "cancelled" });
                if (options.method === "HEAD") return position();
                if (options.method === "PATCH") {
                    chunks++;
                    return received();
                }
                if (url.endsWith("/complete")) {
                    completions++;
                    return Response.json({ id: "cancelled-receipt" });
                }
                snapshotStarted.resolve();
                return snapshot.promise;
            }),
        );
        const manager = managerForTest();
        manager.add([new File(["data"], "cancel.bin")], "/", 10);
        manager.snapshot()[0].uploadId = "cancelled-receipt";
        manager.setEnabled(true);
        await snapshotStarted.promise;
        const key = manager.snapshot()[0].key;
        const cancelling = manager.cancel(key);
        expect(manager.snapshot()[0].state).toBe("cancelling");

        snapshot.resolve(receipt("cancelled-receipt"));
        await cancelling;
        await vi.waitFor(() =>
            expect(manager.snapshot()[0].controller).toBeUndefined(),
        );
        expect(manager.snapshot()[0].state).toBe("cancelled");
        expect(manager.snapshot()[0].file).toBeUndefined();
        expect(chunks).toBe(0);
        expect(completions).toBe(0);
    });

    it.each(["success", "failure"])(
        "preserves a polled completion when its delayed receipt response is a %s",
        async (response) => {
            const completion = deferred<Response>();
            const completionStarted = deferred<void>();
            const changes = vi.fn();
            window.addEventListener("tgdrive:files-changed", changes);
            let accepted = false;
            vi.stubGlobal(
                "fetch",
                vi.fn(async (url: string, options: RequestInit = {}) => {
                    if (url === "/api/v1/jobs")
                        return Response.json({
                            jobs: accepted
                                ? [
                                      {
                                          id: "already-completed",
                                          kind: "upload",
                                          status: "completed",
                                          phase: "completed",
                                          current: 4,
                                          total: 4,
                                      },
                                  ]
                                : [],
                        });
                    if (url === "/api/v1/uploads")
                        return receipt("already-completed");
                    if (options.method === "HEAD") return position();
                    if (options.method === "PATCH") return received();
                    if (url.endsWith("/complete")) {
                        accepted = true;
                        completionStarted.resolve();
                        return completion.promise;
                    }
                    return receipt("already-completed");
                }),
            );
            const manager = managerForTest();
            manager.add([new File(["data"], "complete.bin")], "/", 10);
            manager.setEnabled(true);
            await completionStarted.promise;
            manager.refreshJobs();
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].state).toBe("completed"),
            );
            expect(manager.snapshot()[0].current).toBe(4);

            const laterStates: string[] = [];
            manager.subscribe(() =>
                laterStates.push(manager.snapshot()[0].state),
            );
            completion.resolve(
                response === "success"
                    ? Response.json({ id: "already-completed" })
                    : Response.json(
                          { detail: "Receipt response lost at the proxy" },
                          { status: 503 },
                      ),
            );
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].controller).toBeUndefined(),
            );
            expect(manager.snapshot()[0]).toMatchObject({
                jobId: "already-completed",
                state: "completed",
                phase: "Available in your drive",
                current: 4,
                total: 4,
                file: undefined,
            });
            expect(laterStates.every((state) => state === "completed")).toBe(
                true,
            );
            expect(changes).toHaveBeenCalledOnce();
        },
    );

    it("tries smaller queued files and retries staging on its own timer while polling is unavailable", async () => {
        vi.useFakeTimers();
        const progress = deferred<Response>();
        const attempts = new Map<string, number>();
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string, options: RequestInit = {}) => {
                if (url === "/api/v1/jobs") return progress.promise;
                if (url === "/api/v1/uploads") {
                    const { name } = JSON.parse(String(options.body)) as {
                        name: string;
                    };
                    const attempt = (attempts.get(name) || 0) + 1;
                    attempts.set(name, attempt);
                    if (name !== "small.bin" && attempt === 1)
                        return Response.json(
                            { detail: "Staging is full" },
                            { status: 507 },
                        );
                    return receipt(name, name === "small.bin" ? 4 : 8);
                }
                const id = url.split("/")[4];
                const size = id === "small.bin" ? 4 : 8;
                if (options.method === "HEAD") return position(size);
                if (options.method === "PATCH")
                    return received(
                        Number(
                            new Headers(options.headers).get("Upload-Offset"),
                        ) + (options.body as Blob).size,
                    );
                if (url.endsWith("/complete")) return Response.json({ id });
                return receipt(id, size);
            }),
        );
        const manager = managerForTest();
        manager.add(
            [
                ...Array.from(
                    { length: BROWSER_RECEIVE_CONCURRENCY },
                    (_, index) =>
                        new File(["big data"], "full-" + index + ".bin"),
                ),
                new File(["data"], "small.bin"),
            ],
            "/",
            10,
        );
        try {
            manager.setEnabled(true);
            await vi.advanceTimersByTimeAsync(0);
            expect(attempts.size).toBe(BROWSER_RECEIVE_CONCURRENCY + 1);
            expect(
                manager.snapshot().find((item) => item.name === "small.bin")
                    ?.jobId,
            ).toBe("small.bin");
            expect(
                manager.snapshot().filter((item) => item.state === "queued"),
            ).toHaveLength(BROWSER_RECEIVE_CONCURRENCY);

            await vi.advanceTimersByTimeAsync(4999);
            expect(
                [...attempts.values()].filter((count) => count > 1),
            ).toHaveLength(0);
            await vi.advanceTimersByTimeAsync(1);
            expect([...attempts.values()]).toEqual([
                ...Array(BROWSER_RECEIVE_CONCURRENCY).fill(2),
                1,
            ]);
            expect(
                manager.snapshot().every((item) => Boolean(item.jobId)),
            ).toBe(true);
        } finally {
            manager.setEnabled(false);
            progress.resolve(Response.json({ jobs: [] }));
            await vi.advanceTimersByTimeAsync(0);
        }
    });

    it("starts with two device slots and fills a cancelled slot immediately", async () => {
        let active = 0;
        let maximum = 0;
        const pending = new Map<string, () => void>();
        const started: string[] = [];
        const sendChunk: UploadChunkTransport = (url, options) =>
            new Promise<Response>((resolve, reject) => {
                const id = url.split("/")[4];
                let settled = false;
                const finish = (aborted = false) => {
                    if (settled) return;
                    settled = true;
                    options.signal.removeEventListener("abort", abort);
                    pending.delete(id);
                    active--;
                    if (aborted)
                        reject(new DOMException("Upload paused", "AbortError"));
                    else resolve(received());
                };
                const abort = () => finish(true);
                active++;
                maximum = Math.max(maximum, active);
                started.push(id);
                pending.set(id, finish);
                options.signal.addEventListener("abort", abort, { once: true });
                if (options.signal.aborted) abort();
            });
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string, options: RequestInit = {}) => {
                if (url === "/api/v1/jobs") return Response.json({ jobs: [] });
                if (url === "/api/v1/uploads") {
                    const { name } = JSON.parse(String(options.body)) as {
                        name: string;
                    };
                    return receipt(name);
                }
                if (url.endsWith("/cancel"))
                    return Response.json({ state: "cancelled" });
                if (options.method === "HEAD") return position();
                const id = url.split("/")[4];
                if (url.endsWith("/complete")) return Response.json({ id });
                return receipt(id);
            }),
        );
        const manager = managerForTest(sendChunk);
        manager.add(
            Array.from(
                { length: BROWSER_RECEIVE_INITIAL_CONCURRENCY + 1 },
                (_, index) => new File(["data"], "file-" + index + ".bin"),
            ),
            "/",
            10,
        );
        manager.setEnabled(true);
        await vi.waitFor(() =>
            expect(active).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY),
        );
        expect(started).toHaveLength(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
        await manager.cancel(manager.snapshot()[0].key);
        await vi.waitFor(() =>
            expect(started).toHaveLength(
                BROWSER_RECEIVE_INITIAL_CONCURRENCY + 1,
            ),
        );
        expect(active).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
        expect(maximum).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
        expect(manager.snapshot()[0].state).toBe("cancelled");
        for (const finish of [...pending.values()]) finish();
        await vi.waitFor(() =>
            expect(
                manager.snapshot().filter((item) => Boolean(item.jobId)),
            ).toHaveLength(BROWSER_RECEIVE_INITIAL_CONCURRENCY),
        );
        expect(new Set(started).size).toBe(
            BROWSER_RECEIVE_INITIAL_CONCURRENCY + 1,
        );
        expect(maximum).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
    });
});

describe("upload responsiveness", () => {
    it.each(["sent", "cancelled", "error", "disabled"])(
        "coalesces byte progress and immediately handles %s",
        async (transition) => {
            vi.useFakeTimers();
            const chunk = deferred<Response>();
            let report: ((bytes: number) => void) | undefined;
            vi.stubGlobal(
                "fetch",
                vi.fn(async (url: string) => {
                    if (url === "/api/v1/jobs")
                        return Response.json({ jobs: [] });
                    if (url.endsWith("/cancel"))
                        return Response.json({ state: "cancelled" });
                    return receipt("progress");
                }),
            );
            const manager = managerForTest((_url, _options, progress) => {
                report = progress;
                return chunk.promise;
            });
            manager.add([new File(["data"], "progress.bin")], "/", 10);
            manager.setEnabled(true);
            await vi.advanceTimersByTimeAsync(0);
            expect(report).toBeDefined();
            const published = vi.fn();
            manager.subscribe(published);
            report!(1);
            report!(2);
            expect(manager.snapshot()[0].sentCurrent).toBe(2);
            expect(published).not.toHaveBeenCalled();
            await vi.advanceTimersByTimeAsync(249);
            expect(published).not.toHaveBeenCalled();
            await vi.advanceTimersByTimeAsync(1);
            expect(published).toHaveBeenCalledOnce();
            report!(3);
            expect(published).toHaveBeenCalledOnce();
            if (transition === "sent") {
                report!(4);
                expect(published).toHaveBeenCalledTimes(2);
                expect(manager.snapshot()[0].phase).toContain(
                    "Waiting for server confirmation",
                );
                const stored = JSON.parse(
                    localStorage.getItem(
                        "tgdrive.uploads.v2:" + manager.snapshot()[0].key,
                    )!,
                );
                expect(stored.current).toBe(0);
                expect(stored.sentCurrent).toBeUndefined();
            } else if (transition === "cancelled") {
                const cancelling = manager.cancel(manager.snapshot()[0].key);
                expect(manager.snapshot()[0].state).toBe("cancelling");
                expect(published.mock.calls.length).toBeGreaterThan(1);
                chunk.resolve(received());
                await cancelling;
                await vi.advanceTimersByTimeAsync(0);
                expect(manager.snapshot()[0].state).toBe("cancelled");
            } else if (transition === "error") {
                chunk.resolve(
                    Response.json({ detail: "Rejected" }, { status: 403 }),
                );
                await vi.advanceTimersByTimeAsync(0);
                expect(manager.snapshot()[0].state).toBe("error");
                expect(published.mock.calls.length).toBeGreaterThan(1);
            } else manager.setEnabled(false);
            const notifications = published.mock.calls.length;
            await vi.advanceTimersByTimeAsync(250);
            expect(published).toHaveBeenCalledTimes(notifications);
        },
    );

    it.each([false, true])(
        "bounds completion-driven polls while the initial poll is held=%s and keeps explicit refresh immediate",
        async (held) => {
            vi.useFakeTimers();
            const initial = deferred<Response>();
            let polls = 0;
            let created = 0;
            vi.stubGlobal(
                "fetch",
                vi.fn(async (url: string) => {
                    if (url === "/api/v1/jobs") {
                        polls++;
                        if (held && polls === 1) return initial.promise;
                        return Response.json({ jobs: [] });
                    }
                    if (url === "/api/v1/uploads")
                        return receipt("small-" + ++created);
                    if (url.endsWith("/complete"))
                        return Response.json({ id: url.split("/")[4] });
                    return received();
                }),
            );
            const manager = managerForTest();
            manager.add(
                Array.from(
                    { length: 8 },
                    (_, index) => new File(["data"], "small-" + index),
                ),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.advanceTimersByTimeAsync(0);
            expect(manager.snapshot().every((item) => item.jobId)).toBe(true);
            initial.resolve(Response.json({ jobs: [] }));
            await vi.advanceTimersByTimeAsync(0);
            expect(polls).toBe(1);
            await vi.advanceTimersByTimeAsync(1499);
            expect(polls).toBe(1);
            await vi.advanceTimersByTimeAsync(1);
            expect(polls).toBe(2);
            manager.refreshJobs();
            await vi.advanceTimersByTimeAsync(0);
            expect(polls).toBe(3);
        },
    );

    it("persists an acknowledged chunk immediately without scanning queued recovery records", async () => {
        vi.useFakeTimers();
        const firstChunk = deferred<Response>();
        const nextChunk = deferred<Response>();
        let sent = 0;
        let created = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string) =>
                url === "/api/v1/jobs"
                    ? Response.json({ jobs: [] })
                    : receipt("acknowledged-" + ++created, 8),
            ),
        );
        const manager = managerForTest(() =>
            ++sent === 1 ? firstChunk.promise : nextChunk.promise,
        );
        manager.add(
            Array.from(
                { length: 100 },
                (_, index) => new File(["abcdefgh"], "source-" + index),
            ),
            "/",
            10,
        );
        manager.setEnabled(true);
        await vi.advanceTimersByTimeAsync(0);
        const scans = vi.spyOn(localStorage, "key");
        const writes = vi.spyOn(localStorage, "setItem");
        firstChunk.resolve(received(4));
        await vi.advanceTimersByTimeAsync(0);
        expect(sent).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY + 1);
        expect(manager.snapshot()[0].current).toBe(4);
        expect(scans).not.toHaveBeenCalled();
        expect(writes).toHaveBeenCalledOnce();
        const stored = JSON.parse(writes.mock.calls[0][1]);
        expect(stored.current).toBe(4);
        expect(stored.sentCurrent).toBeUndefined();
        scans.mockRestore();
        writes.mockRestore();
    });

    it("updates only a polled receipt, preserves storage warnings and respects a cleared record", async () => {
        vi.useFakeTimers();
        for (let index = 0; index < 100; index++)
            localStorage.setItem(
                "tgdrive.uploads.v2:record-" + index,
                JSON.stringify({
                    key: "record-" + index,
                    initiatedHere: true,
                    name: "file-" + index,
                    destination: "/",
                    size: 8,
                    total: 8,
                    current: 0,
                    lastModified: 0,
                    state: "awaiting_file",
                    phase: "Choose the original file to resume",
                    ...(index === 0 ? { jobId: "job" } : {}),
                }),
            );
        localStorage.setItem("tgdrive.uploads.v2:broken", "{invalid");
        let current = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "job",
                            kind: "upload",
                            status: "running",
                            phase: "uploading",
                            total: 8,
                            current,
                        },
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        manager.setEnabled(true);
        await vi.advanceTimersByTimeAsync(0);
        const scans = vi.spyOn(localStorage, "key");
        const reads = vi.spyOn(localStorage, "getItem");
        const writes = vi.spyOn(localStorage, "setItem");
        current = 4;
        manager.refreshJobs();
        await vi.advanceTimersByTimeAsync(0);
        expect(scans).not.toHaveBeenCalled();
        expect(reads.mock.calls.map(([key]) => key)).toEqual([
            "tgdrive.uploads.v2:record-0",
        ]);
        expect(writes).toHaveBeenCalledOnce();
        expect(JSON.parse(writes.mock.calls[0][1]).current).toBe(4);
        expect(manager.recoveryWarning).toContain(
            "Some transfer recovery information",
        );
        writes.mockImplementationOnce(() => {
            throw new Error("Storage temporarily unavailable");
        });
        current = 5;
        manager.refreshJobs();
        await vi.advanceTimersByTimeAsync(0);
        expect(manager.recoveryWarning).toContain("could not save");
        expect(
            JSON.parse(localStorage.getItem("tgdrive.uploads.v2:record-0")!)
                .current,
        ).toBe(4);
        writes.mockClear();
        manager.refreshJobs();
        await vi.advanceTimersByTimeAsync(0);
        expect(writes).toHaveBeenCalledOnce();
        expect(JSON.parse(writes.mock.calls[0][1]).current).toBe(5);
        expect(manager.recoveryWarning).toContain(
            "Some transfer recovery information",
        );
        localStorage.setItem("tgdrive.uploads.v2:record-0", "null");
        writes.mockClear();
        current = 6;
        manager.refreshJobs();
        await vi.advanceTimersByTimeAsync(0);
        expect(writes).not.toHaveBeenCalled();
        expect(manager.snapshot().some((item) => item.key === "record-0")).toBe(
            false,
        );
        expect(localStorage.getItem("tgdrive.uploads.v2:record-0")).toBe("null");
        scans.mockRestore();
        reads.mockRestore();
        writes.mockRestore();
    });

    it.each([
        "{invalid",
        "42",
        JSON.stringify({
            key: "another-record",
            initiatedHere: true,
            name: "source.bin",
            destination: "/",
            size: 8,
        }),
    ])(
        "keeps a recovery warning when a changed record contains %s",
        async (malformed) => {
            vi.useFakeTimers();
            let current = 0;
            vi.stubGlobal(
                "fetch",
                vi.fn(async () =>
                    Response.json({
                        jobs: [
                            {
                                id: "job",
                                status: "running",
                                phase: "uploading",
                                total: 8,
                                current,
                            },
                        ],
                    }),
                ),
            );
            const manager = managerForTest();
            manager.addJob("job", "source.bin", "/");
            manager.setEnabled(true);
            await vi.advanceTimersByTimeAsync(0);
            const key = "tgdrive.uploads.v2:" + manager.snapshot()[0].key;
            localStorage.setItem(key, malformed);
            current = 4;
            manager.refreshJobs();
            await vi.advanceTimersByTimeAsync(0);
            expect(manager.recoveryWarning).toContain(
                "Some transfer recovery information",
            );
            expect(JSON.parse(localStorage.getItem(key)!).current).toBe(4);
        },
    );
});
