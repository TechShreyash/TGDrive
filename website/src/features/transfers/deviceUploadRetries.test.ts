import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubDeviceLocks } from "./transferTestLocks";
import { TransferManager } from "./transfers";
import type { Job } from "../../shared/types";
import type { UploadChunkTransport } from "./uploadTransport";

const managers: TransferManager[] = [];
type PendingChunk = {
    offset: number;
    signal: AbortSignal;
    resolve: (response: Response) => void;
    reject: (error: unknown) => void;
};

async function deviceUpload() {
    const pending: PendingChunk[] = [];
    let offset = 0;
    let jobs: Job[] = [];
    let createStatus = 201;
    let headFailure: unknown;
    let inspectionOffset: number | undefined;
    let completionFailure = false;
    const requests: string[] = [];
    const receipt = () => ({
        id: "receipt",
        offset,
        total_size: 8,
        chunk_size: 4,
        checksums: Array.from({ length: offset / 4 }, (_, index) => ({
            offset: index * 4,
            size: 4,
            sha256: "0102",
        })),
    });
    vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, options: RequestInit = {}) => {
            requests.push((options.method || "GET") + " " + url);
            if (url === "/api/v1/jobs") return Response.json({ jobs });
            if (url === "/api/v1/uploads")
                return createStatus === 201
                    ? Response.json(receipt(), { status: 201 })
                    : Response.json(
                          { detail: "Creation interrupted" },
                          { status: createStatus },
                      );
            if (options.method === "HEAD") {
                if (headFailure) throw headFailure;
                expect(options.signal).toBeInstanceOf(AbortSignal);
                return new Response(null, {
                    headers: {
                        "Upload-Offset": String(offset),
                        "Upload-Length": "8",
                    },
                });
            }
            if (url.endsWith("/complete")) {
                if (completionFailure)
                    throw new TypeError("Completion response lost");
                return Response.json({ id: "receipt" });
            }
            if (url.endsWith("/cancel"))
                return Response.json({ state: "cancelled" });
            if (url === "/api/v1/uploads/receipt") {
                if (inspectionOffset !== undefined) offset = inspectionOffset;
                return Response.json(receipt());
            }
            throw new Error("Unexpected request: " + url);
        }),
    );
    const send: UploadChunkTransport = (_url, options) =>
        new Promise<Response>((resolve, reject) => {
            pending.push({
                offset: Number(
                    new Headers(options.headers).get("Upload-Offset"),
                ),
                signal: options.signal,
                resolve,
                reject,
            });
            options.signal.addEventListener(
                "abort",
                () => reject(new DOMException("Upload paused", "AbortError")),
                { once: true },
            );
        });
    const manager = new TransferManager(send);
    managers.push(manager);
    const flush = () => vi.advanceTimersByTimeAsync(0);
    const start = async () => {
        manager.add([new File(["abcdefgh"], "sample.bin")], "/", 10);
        manager.setEnabled(true);
        await flush();
    };
    const fail = async (status = 503, retryAfter = 0, code = "") => {
        pending
            .shift()!
            .resolve(
                Response.json(
                    { detail: "Temporarily unavailable", code },
                    { status, headers: { "Retry-After": String(retryAfter) } },
                ),
            );
        await flush();
    };
    const acknowledge = async () => {
        const chunk = pending.shift()!;
        offset = chunk.offset + 4;
        chunk.resolve(
            new Response(null, {
                status: 204,
                headers: { "Upload-Offset": String(offset) },
            }),
        );
        await flush();
    };
    return {
        manager,
        pending,
        requests,
        start,
        flush,
        fail,
        acknowledge,
        setOffset: (value: number) => {
            offset = value;
        },
        setJobs: (value: Job[]) => {
            jobs = value;
        },
        failCreation: (status: number) => {
            createStatus = status;
        },
        failHead: (error: unknown) => {
            headFailure = error;
        },
        commitBeforeInspection: (value: number) => {
            inspectionOffset = value;
        },
        failCompletion: () => {
            completionFailure = true;
        },
    };
}

beforeEach(() => {
    stubDeviceLocks();
    vi.useFakeTimers();
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
    vi.spyOn(crypto.subtle, "digest").mockResolvedValue(
        new Uint8Array([1, 2]).buffer,
    );
});

afterEach(async () => {
    for (const manager of managers.splice(0)) manager.setEnabled(false);
    await vi.advanceTimersByTimeAsync(0);
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("device receipt retries", () => {
    it.each([0, 408, 500, 502, 503, 504])(
        "reconciles a known receipt after a transient %s failure without recreating it",
        async (status) => {
            const upload = await deviceUpload();
            await upload.start();
            if (status) await upload.fail(status);
            else {
                upload.pending
                    .shift()!
                    .reject(new TypeError("Connection interrupted"));
                await upload.flush();
            }
            expect(upload.manager.snapshot()[0]).toMatchObject({
                state: "queued",
                stage: "retry_wait",
                current: 0,
            });
            expect(upload.manager.snapshot()[0].error).toBeUndefined();
            await vi.advanceTimersByTimeAsync(1999);
            expect(upload.pending).toHaveLength(0);
            await vi.advanceTimersByTimeAsync(1);
            expect(upload.pending[0].offset).toBe(0);
            expect(
                upload.requests.filter(
                    (request) => request === "POST /api/v1/uploads",
                ),
            ).toHaveLength(1);
            expect(upload.requests).toContain("HEAD /api/v1/uploads/receipt");
            expect(upload.requests).toContain("GET /api/v1/uploads/receipt");
        },
    );

    it("verifies a chunk accepted before its response was lost and resumes at the server offset", async () => {
        const upload = await deviceUpload();
        await upload.start();
        upload.setOffset(4);
        upload.pending.shift()!.reject(new TypeError("Acknowledgement lost"));
        await upload.flush();
        await vi.advanceTimersByTimeAsync(2000);
        expect(crypto.subtle.digest).toHaveBeenCalledOnce();
        expect(upload.pending[0].offset).toBe(4);
        expect(upload.manager.snapshot()[0].current).toBe(4);
        await upload.acknowledge();
        expect(upload.manager.snapshot()[0].jobId).toBe("receipt");
    });

    it("uses the newer receipt snapshot when a prior chunk commits between HEAD and inspection", async () => {
        const upload = await deviceUpload();
        await upload.start();
        await upload.fail();
        upload.commitBeforeInspection(4);
        await vi.advanceTimersByTimeAsync(2000);
        expect(crypto.subtle.digest).toHaveBeenCalledOnce();
        expect(upload.pending[0].offset).toBe(4);
        expect(upload.manager.snapshot()[0].current).toBe(4);
    });

    it.each([-1, 12, 4.5])(
        "rejects an invalid inspected offset of %s before checking or sending bytes",
        async (offset) => {
            const upload = await deviceUpload();
            await upload.start();
            await upload.fail();
            upload.commitBeforeInspection(offset);
            await vi.advanceTimersByTimeAsync(2000);
            expect(upload.manager.snapshot()[0]).toMatchObject({
                state: "error",
                error: "The server returned an invalid upload position.",
            });
            expect(crypto.subtle.digest).not.toHaveBeenCalled();
            expect(upload.pending).toHaveLength(0);
        },
    );

    it("reconciles a precise offset conflict while leaving other conflicts for manual retry", async () => {
        const upload = await deviceUpload();
        await upload.start();
        await upload.fail(409, 0, "UPLOAD_OFFSET_CHANGED");
        expect(upload.manager.snapshot()[0].state).toBe("queued");
        upload.setOffset(4);
        await vi.advanceTimersByTimeAsync(2000);
        expect(upload.pending[0].offset).toBe(4);
        await upload.fail(409, 0, "DESTINATION_UNAVAILABLE");
        expect(upload.manager.snapshot()[0].state).toBe("error");
        await vi.advanceTimersByTimeAsync(30_000);
        expect(upload.pending).toHaveLength(0);
    });

    it("retries a stalled PATCH timeout as a transient transport failure", async () => {
        const upload = await deviceUpload();
        await upload.start();
        upload.pending
            .shift()!
            .reject(new DOMException("No progress", "TimeoutError"));
        await upload.flush();
        expect(upload.manager.snapshot()[0]).toMatchObject({
            state: "queued",
            stage: "retry_wait",
        });
        await vi.advanceTimersByTimeAsync(2000);
        expect(upload.pending).toHaveLength(1);
        expect(upload.pending[0].offset).toBe(0);
    });

    it("stops after three retries without acknowledged progress and resets on manual retry", async () => {
        const upload = await deviceUpload();
        await upload.start();
        for (const delay of [2000, 4000, 8000]) {
            await upload.fail();
            await vi.advanceTimersByTimeAsync(delay - 1);
            expect(upload.pending).toHaveLength(0);
            await vi.advanceTimersByTimeAsync(1);
            expect(upload.pending).toHaveLength(1);
        }
        await upload.fail();
        expect(upload.manager.snapshot()[0].state).toBe("error");
        await vi.advanceTimersByTimeAsync(30_000);
        expect(upload.pending).toHaveLength(0);
        await upload.manager.retry(upload.manager.snapshot()[0].key);
        await upload.flush();
        await upload.fail();
        await vi.advanceTimersByTimeAsync(2000);
        expect(upload.pending).toHaveLength(1);
    });

    it("starts a fresh retry allowance after acknowledged payload progress", async () => {
        const upload = await deviceUpload();
        await upload.start();
        for (const delay of [2000, 4000, 8000]) {
            await upload.fail();
            await vi.advanceTimersByTimeAsync(delay);
        }
        await upload.acknowledge();
        expect(upload.pending[0].offset).toBe(4);
        await upload.fail();
        expect(upload.manager.snapshot()[0].state).toBe("queued");
        await vi.advanceTimersByTimeAsync(2000);
        expect(upload.pending[0].offset).toBe(4);
    });

    it("honors a longer Retry-After and cancellation removes scheduled retries", async () => {
        const upload = await deviceUpload();
        await upload.start();
        await upload.fail(503, 20);
        await vi.advanceTimersByTimeAsync(19_999);
        expect(upload.pending).toHaveLength(0);
        await upload.manager.cancel(upload.manager.snapshot()[0].key);
        await vi.advanceTimersByTimeAsync(20_000);
        expect(upload.pending).toHaveLength(0);
        expect(upload.manager.snapshot()[0].state).toBe("cancelled");
        expect(
            upload.requests.filter((request) => request.startsWith("HEAD ")),
        ).toHaveLength(0);
    });

    it("does not replay receipt creation when its result is uncertain", async () => {
        const upload = await deviceUpload();
        upload.failCreation(503);
        await upload.start();
        await vi.advanceTimersByTimeAsync(30_000);
        expect(upload.manager.snapshot()[0]).toMatchObject({ state: "error" });
        expect(upload.manager.snapshot()[0].uploadId).toBeUndefined();
        expect(
            upload.requests.filter(
                (request) => request === "POST /api/v1/uploads",
            ),
        ).toHaveLength(1);
        expect(upload.pending).toHaveLength(0);
    });

    it("retries a timed-out offset request through the same bounded retry policy", async () => {
        const upload = await deviceUpload();
        await upload.start();
        await upload.fail();
        upload.failHead(new DOMException("Timed out", "TimeoutError"));
        await vi.advanceTimersByTimeAsync(2000);
        expect(upload.manager.snapshot()[0]).toMatchObject({
            state: "queued",
            stage: "retry_wait",
        });
        upload.failHead(undefined);
        await vi.advanceTimersByTimeAsync(3999);
        expect(upload.pending).toHaveLength(0);
        await vi.advanceTimersByTimeAsync(1);
        expect(upload.pending).toHaveLength(1);
    });

    it("keeps a late server job authoritative after a completion response is lost", async () => {
        const upload = await deviceUpload();
        await upload.start();
        upload.failCompletion();
        await upload.acknowledge();
        await upload.acknowledge();
        expect(upload.manager.snapshot()[0].state).toBe("queued");
        upload.setJobs([
            {
                id: "receipt",
                status: "running",
                phase: "uploading",
                current: 0,
                total: 8,
                name: "sample.bin",
                destination: "/",
            },
        ]);
        upload.manager.refreshJobs();
        await upload.flush();
        await vi.advanceTimersByTimeAsync(10_000);
        expect(upload.manager.snapshot()[0]).toMatchObject({
            jobId: "receipt",
            state: "uploading",
        });
        expect(upload.manager.snapshot()[0].file).toBeUndefined();
        expect(
            upload.requests.filter((request) => request.endsWith("/complete")),
        ).toHaveLength(1);
    });
});
