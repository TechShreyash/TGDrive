import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubDeviceLocks } from "./transferTestLocks";
import { TransferManager } from "./transfers";
import type { UploadChunkTransport } from "./uploadTransport";

const FILE_SIZE = 8 * 1024 * 1024;
const managers: TransferManager[] = [];

interface PendingChunk {
    sent: number;
    progress: (bytes: number) => void;
    succeed: () => void;
    fail: (error: unknown) => void;
    respond: (response: Response) => void;
    signal: AbortSignal;
}

/** Real manager scheduling with controllable browser byte events and responses. */
async function runningUploads(count = 10, resumeFirstFile = false) {
    const chunks = new Map<string, PendingChunk>();
    const started: string[] = [];
    const aborted: string[] = [];
    const sendChunk: UploadChunkTransport = (url, options, progress) =>
        new Promise<Response>((resolve, reject) => {
            const id = decodeURIComponent(url.split("/")[4]);
            let settled = false;
            const settle = (response?: Response, error?: unknown) => {
                if (settled) return;
                settled = true;
                options.signal.removeEventListener("abort", abort);
                chunks.delete(id);
                if (error !== undefined) reject(error);
                else resolve(response!);
            };
            const abort = () => {
                aborted.push(id);
                settle(
                    undefined,
                    new DOMException("Upload paused", "AbortError"),
                );
            };
            chunks.set(id, {
                sent: 0,
                progress,
                succeed: () =>
                    settle(
                        new Response(null, {
                            status: 204,
                            headers: { "Upload-Offset": String(FILE_SIZE) },
                        }),
                    ),
                fail: (error) => settle(undefined, error),
                respond: (response) => settle(response),
                signal: options.signal,
            });
            started.push(id);
            options.signal.addEventListener("abort", abort, { once: true });
            if (options.signal.aborted) abort();
        });
    const receipt = (id: string) =>
        Response.json({
            id,
            offset: resumeFirstFile && id === "file-0.bin" ? 4 : 0,
            total_size: FILE_SIZE,
            chunk_size: FILE_SIZE,
            checksums:
                resumeFirstFile && id === "file-0.bin"
                    ? [
                          {
                              offset: 0,
                              size: 4,
                              sha256: "unused-before-digest-fails",
                          },
                      ]
                    : [],
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
            const id = decodeURIComponent(url.split("/")[4]);
            if (url.endsWith("/cancel"))
                return Response.json({ state: "cancelled" });
            if (options.method === "HEAD")
                return new Response(null, {
                    headers: {
                        "Upload-Offset":
                            resumeFirstFile && id === "file-0.bin" ? "4" : "0",
                        "Upload-Length": String(FILE_SIZE),
                    },
                });
            if (url.endsWith("/complete")) return Response.json({ id });
            return receipt(id);
        }),
    );
    const manager = new TransferManager(sendChunk);
    managers.push(manager);
    const contents = new Uint8Array(FILE_SIZE);
    manager.add(
        Array.from(
            { length: count },
            (_, index) => new File([contents], `file-${index}.bin`),
        ),
        "/",
        FILE_SIZE,
    );
    if (resumeFirstFile)
        Object.assign(manager.snapshot()[0], {
            uploadId: "file-0.bin",
            current: 4,
        });
    manager.setEnabled(true);
    await vi.advanceTimersByTimeAsync(0);
    const flush = () => vi.advanceTimersByTimeAsync(0);
    const observedConcurrency: number[] = [chunks.size];
    const flow = async (
        seconds: number,
        bytesPerStream: (active: number) => number,
    ) => {
        for (let second = 0; second < seconds; second++) {
            await vi.advanceTimersByTimeAsync(1000);
            const bytes = bytesPerStream(chunks.size);
            for (const chunk of [...chunks.values()]) {
                chunk.sent = Math.min(FILE_SIZE - 1, chunk.sent + bytes);
                chunk.progress(chunk.sent);
            }
            await flush();
            observedConcurrency.push(chunks.size);
        }
    };
    return {
        manager,
        chunks,
        started,
        aborted,
        flush,
        flow,
        observedConcurrency,
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
});

afterEach(async () => {
    for (const manager of managers.splice(0)) manager.setEnabled(false);
    await vi.advanceTimersByTimeAsync(0);
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("adaptive device scheduling integration", () => {
    it("starts two requests and does not grow just because files remain queued", async () => {
        const upload = await runningUploads();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toHaveLength(2);

        await vi.advanceTimersByTimeAsync(90_000);
        expect(upload.started).toHaveLength(2);
    });

    it("probes three then four only while more streams improve aggregate byte throughput", async () => {
        const upload = await runningUploads();
        await upload.flow(40, () => 64 * 1024);

        expect(upload.observedConcurrency).toContain(3);
        expect(upload.observedConcurrency).toContain(4);
        expect(upload.observedConcurrency.indexOf(3)).toBeLessThan(
            upload.observedConcurrency.indexOf(4),
        );
        expect(Math.max(...upload.observedConcurrency)).toBe(4);
        expect(upload.started).toHaveLength(4);
        expect(upload.aborted).toEqual([]);
    });

    it("rolls back a probe that merely divides the same total bandwidth", async () => {
        const upload = await runningUploads();
        await upload.flow(30, (active) => Math.floor((128 * 1024) / active));

        expect(upload.observedConcurrency).toContain(3);
        expect(upload.observedConcurrency).not.toContain(4);
        // An admission reduction leaves the three already-started requests alone.
        expect(upload.chunks.size).toBe(3);
        expect(upload.aborted).toEqual([]);
        const first = [...upload.chunks.values()][0];
        first.succeed();
        await upload.flush();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toHaveLength(3);
        [...upload.chunks.values()][0].succeed();
        await upload.flush();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toHaveLength(4);
    });

    it("replaces a cancelled request without reducing the healthy device limit", async () => {
        const upload = await runningUploads();
        const cancelled = upload.manager.snapshot()[0];
        await upload.manager.cancel(cancelled.key);
        await upload.flush();

        expect(upload.manager.snapshot()[0].state).toBe("cancelled");
        expect(upload.started).toHaveLength(3);
        expect(upload.chunks.size).toBe(2);
        expect(upload.aborted).toEqual([cancelled.name]);
        expect(
            [...upload.chunks.values()].every((chunk) => !chunk.signal.aborted),
        ).toBe(true);
    });

    it.each(["network", "server"])(
        "reduces new admissions after a %s error while leaving other requests running",
        async (failure) => {
            const upload = await runningUploads();
            const [failed, remaining] = [...upload.chunks.values()];
            if (failure === "network")
                failed.fail(new TypeError("Network interrupted"));
            else
                failed.respond(
                    Response.json({ detail: "Busy" }, { status: 503 }),
                );
            await upload.flush();

            expect(upload.started).toHaveLength(2);
            expect(upload.chunks.size).toBe(1);
            expect(remaining.signal.aborted).toBe(false);
            expect(upload.aborted).toEqual([]);

            remaining.succeed();
            await upload.flush();
            expect(upload.started).toHaveLength(3);
            expect(upload.chunks.size).toBe(1);
            expect(upload.manager.snapshot()[1].jobId).toBe("file-1.bin");
        },
    );

    it("keeps device capacity available for smaller files when a receipt hits staging pressure", async () => {
        const upload = await runningUploads();
        [...upload.chunks.values()][0].respond(
            Response.json({ detail: "Staging is full" }, { status: 507 }),
        );
        await upload.flush();

        expect(upload.manager.snapshot()[0]).toMatchObject({
            state: "queued",
            phase: "Waiting for server space",
        });
        expect(upload.started).toHaveLength(3);
        expect(upload.chunks.size).toBe(2);
        expect(upload.aborted).toEqual([]);
    });

    it("does not classify a resume verification runtime failure as network pressure", async () => {
        const digest = vi
            .spyOn(crypto.subtle, "digest")
            .mockRejectedValueOnce(new TypeError("Checksum runtime error"));
        const upload = await runningUploads(10, true);
        await upload.flush();

        expect(digest).toHaveBeenCalledOnce();
        expect(upload.manager.snapshot()[0]).toMatchObject({
            state: "error",
            error: "Checksum runtime error",
        });
        expect(upload.started).toEqual(["file-1.bin", "file-2.bin"]);
        expect(upload.chunks.size).toBe(2);

        upload.chunks.get("file-1.bin")!.succeed();
        await upload.flush();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toEqual([
            "file-1.bin",
            "file-2.bin",
            "file-3.bin",
        ]);
        expect(upload.aborted).toEqual([]);
    });

    it("discards background byte and timer samples and starts a fresh visible window", async () => {
        const visibility = Object.assign(new EventTarget(), {
            visibilityState: "visible",
        });
        vi.stubGlobal("document", visibility);
        const upload = await runningUploads();
        await upload.flow(4, () => 64 * 1024);

        visibility.visibilityState = "hidden";
        await upload.flow(40, () => 64 * 1024);
        await vi.advanceTimersByTimeAsync(30_000);
        expect(upload.started).toHaveLength(2);
        expect(upload.aborted).toEqual([]);

        visibility.visibilityState = "visible";
        await upload.flow(1, () => 64 * 1024);
        expect(upload.started).toHaveLength(2);

        // A new request still fills the healthy two-file limit, proving the hidden
        // no-progress interval did not count as a network stall and reduce it.
        [...upload.chunks.values()][0].succeed();
        await upload.flush();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toHaveLength(3);
        await upload.flow(3, () => 64 * 1024);
        expect(upload.started).toHaveLength(3);
    });

    it("does not treat a fully sent body waiting for acknowledgement as a device stall", async () => {
        const upload = await runningUploads();
        for (const chunk of upload.chunks.values()) chunk.progress(FILE_SIZE);
        await vi.advanceTimersByTimeAsync(60_000);
        expect(upload.started).toHaveLength(2);
        expect(upload.aborted).toEqual([]);

        [...upload.chunks.values()][0].succeed();
        await upload.flush();
        expect(upload.chunks.size).toBe(2);
        expect(upload.started).toHaveLength(3);
    });
});
