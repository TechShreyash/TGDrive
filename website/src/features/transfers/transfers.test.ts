import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    fromJob,
    safeTransferMetadata,
    uploadBackupState,
    verifyReceivedChunks,
    TransferManager,
    retainTransferBatches,
    BROWSER_RECEIVE_INITIAL_CONCURRENCY,
    type Transfer,
} from "./transfers";
import { transferTaskId } from "./transferTasks";
import { stubDeviceLocks } from "./transferTestLocks";
import {
    fileUrl,
    itemPath,
    formatBytes,
    formatByteProgress,
    remainingBytesHint,
    type Job,
} from "../../shared/types";
const managerForTest = () =>
    new TransferManager((url, options) => fetch(url, options));

function folderBatch(name: string, rootPath = "/" + name) {
    return {
        name,
        folderUpload: {
            id: crypto.randomUUID(),
            name,
            root_path: rootPath,
            created_at: Date.now() / 1000,
        },
    };
}

async function hash(data: string) {
    return [
        ...new Uint8Array(
            await crypto.subtle.digest(
                "SHA-256",
                new TextEncoder().encode(data),
            ),
        ),
    ]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
}
beforeEach(() => {
    stubDeviceLocks();
    const data = new Map<string, string>();
    vi.stubGlobal("localStorage", {
        get length() {
            return data.size;
        },
        key: (index: number) => [...data.keys()][index] ?? null,
        removeItem: (key: string) => data.delete(key),
        getItem: (key: string) => data.get(key) || null,
        setItem: (key: string, value: string) => data.set(key, value),
    });
    vi.stubGlobal("window", new EventTarget());
});
afterEach(() => vi.unstubAllGlobals());
describe("manual transfer history clearing", () => {
    it("keeps reloaded browser-only errors and their completed folder members recoverable", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ cleared: 0 })),
        );
        const first = managerForTest();
        const folder = folderBatch("Folder");
        first.addEntries(
            ["pending.bin", "done.bin"].map((name) => ({
                file: new File(["data"], name),
                destination: "/Folder",
                relativePath: "Folder/" + name,
            })),
            10,
            true,
            folder,
        );
        Object.assign(first.snapshot()[0], {
            state: "error",
            uploadId: "partial-receipt",
            current: 2,
        });
        Object.assign(first.snapshot()[1], {
            state: "completed",
            jobId: "finished-member",
        });
        first.clearFinished();
        const restored = managerForTest();
        expect(restored.snapshot()[0].state).toBe("error");
        expect(restored.snapshot()[0].file).toBeUndefined();
        await restored.clearHistory();
        expect(fetch).toHaveBeenCalledWith(
            "/api/v1/transfers/history",
            expect.objectContaining({
                body: JSON.stringify({
                    exclude_folders: [folder.folderUpload.id],
                }),
            }),
        );
        expect(managerForTest().snapshot()).toMatchObject([
            {
                name: "pending.bin",
                uploadId: "partial-receipt",
                current: 2,
                state: "error",
            },
            { name: "done.bin", jobId: "finished-member", state: "completed" },
        ]);
    });

    function seedHistory(manager: TransferManager) {
        manager.addEntries(
            ["completed", "cancelled", "error", "queued", "awaiting_file"].map(
                (name) => ({ file: new File(["x"], name), destination: "/" }),
            ),
            10,
            true,
        );
        const states = [
            "completed",
            "cancelled",
            "error",
            "queued",
            "awaiting_file",
        ] as const;
        manager.snapshot().forEach((item, index) => {
            item.state = states[index];
            if (index < 3) item.jobId = "job-" + item.name;
        });
        const folderUpload = {
            id: "unfinished-folder-0001",
            name: "Folder",
            root_path: "/folder",
            created_at: 1000,
        };
        manager.addEntries(
            ["first", "second"].map((name) => ({
                file: new File(["x"], name),
                destination: "/folder",
                relativePath: "Folder/" + name,
            })),
            10,
            true,
            { name: "Folder", folderUpload },
        );
        manager.snapshot().find((item) => item.name === "first")!.state =
            "completed";
        return folderUpload;
    }

    it("clears saved history and local finished rows while keeping unfinished folders", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ cleared: 3 })),
        );
        const manager = managerForTest();
        const folder = seedHistory(manager);
        await manager.clearHistory();
        expect(fetch).toHaveBeenCalledWith(
            "/api/v1/transfers/history",
            expect.objectContaining({
                method: "DELETE",
                body: JSON.stringify({ exclude_folders: [folder.id] }),
            }),
        );
        expect(manager.snapshot().map((item) => item.name)).toEqual([
            "queued",
            "awaiting_file",
            "first",
            "second",
        ]);
        expect(
            managerForTest()
                .snapshot()
                .map((item) => item.name),
        ).toEqual(["queued", "awaiting_file", "first", "second"]);
    });

    it("keeps browser history when the server cannot confirm clearing", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    { detail: "History could not be saved" },
                    { status: 503 },
                ),
            ),
        );
        const manager = managerForTest();
        seedHistory(manager);
        const keys = manager.snapshot().map((item) => item.key);
        await expect(manager.clearHistory()).rejects.toThrow(
            "History could not be saved",
        );
        expect(manager.snapshot().map((item) => item.key)).toEqual(keys);
    });

    it("ignores terminal rows from a job poll started before clearing", async () => {
        let resolvePoll!: (response: Response) => void;
        const pending = new Promise<Response>((resolve) => {
            resolvePoll = resolve;
        });
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string) =>
                url === "/api/v1/jobs"
                    ? pending
                    : Response.json({ cleared: 1 }),
            ),
        );
        const manager = managerForTest();
        const published = vi.fn();
        manager.subscribe(published);
        try {
            manager.setEnabled(true);
            await manager.clearHistory();
            published.mockClear();
            resolvePoll(
                Response.json({
                    jobs: [
                        {
                            id: "stale-finished",
                            name: "Old file",
                            kind: "upload",
                            status: "completed",
                            phase: "done",
                            current: 1,
                            total: 1,
                        },
                    ],
                }),
            );
            await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
            await vi.waitFor(() => expect(published).toHaveBeenCalled());
            expect(manager.snapshot()).toEqual([]);
        } finally {
            manager.setEnabled(false);
        }
    });

    it("settles local progress when another browser clears the completed job", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "cleared-elsewhere",
                            name: "Uploading",
                            kind: "upload",
                            status: "completed",
                            phase: "done",
                            current: 1,
                            total: 1,
                            history_cleared: true,
                        },
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        manager.addEntries(
            [{ file: new File(["x"], "Uploading"), destination: "/" }],
            10,
            true,
        );
        Object.assign(manager.snapshot()[0], {
            jobId: "cleared-elsewhere",
            state: "uploading",
            file: undefined,
        });
        const changed = vi.fn();
        window.addEventListener("tgdrive:files-changed", changed);
        try {
            manager.setEnabled(true);
            await vi.waitFor(() => expect(changed).toHaveBeenCalledOnce());
            expect(manager.snapshot()).toEqual([]);
        } finally {
            manager.setEnabled(false);
        }
    });
});
describe("deletion progress", () => {
    const deletion = (status: string, id = "deletion"): Job => ({
        id,
        kind: "delete",
        name: "Old file",
        status,
        phase: status,
        current: 0,
        total: 1,
    });

    it("shows accepted work immediately and retains completion first seen after it finished", async () => {
        const changes = vi.fn();
        window.addEventListener("tgdrive:files-changed", changes);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        deletion("completed", "historic"),
                        deletion("completed"),
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        try {
            manager.trackDeletions([{ id: "deletion", name: "Old file" }]);
            expect(manager.deletionSnapshot().jobs[0].status).toBe("queued");
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().completed).toBe(1),
            );
            expect(changes).toHaveBeenCalledOnce();
            manager.dismissDeletionResult();
            expect(manager.deletionSnapshot().completed).toBe(0);
            manager.refreshJobs();
            await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
            expect(manager.deletionSnapshot().completed).toBe(0);
        } finally {
            manager.setEnabled(false);
        }
    });

    it("does not show success for historical completed jobs after a reload", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ jobs: [deletion("completed")] })),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().jobs).toHaveLength(1),
            );
            expect(manager.deletionSnapshot().completed).toBe(0);
        } finally {
            manager.setEnabled(false);
        }
    });

    it("rechecks a retry after an older poll and ignores its stale failure", async () => {
        let polls = 0;
        let resolveOld!: (response: Response) => void;
        const oldPoll = new Promise<Response>((resolve) => {
            resolveOld = resolve;
        });
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string) => {
                if (url.endsWith("/retry"))
                    return Response.json(deletion("queued"));
                polls++;
                if (polls === 2) return oldPoll;
                return Response.json({
                    jobs: [deletion(polls === 1 ? "error" : "completed")],
                });
            }),
        );
        const manager = managerForTest();
        const statuses: string[] = [];
        const changed = vi.fn();
        window.addEventListener("tgdrive:files-changed", changed);
        manager.subscribe(() =>
            statuses.push(manager.deletionSnapshot().jobs[0]?.status),
        );
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().jobs[0]?.status).toBe(
                    "error",
                ),
            );
            manager.refreshJobs();
            await vi.waitFor(() => expect(polls).toBe(2));
            changed.mockClear();
            await manager.retryDeletion("deletion");
            expect(manager.deletionSnapshot().jobs[0].status).toBe("queued");
            expect(changed).toHaveBeenCalledOnce();
            statuses.length = 0;
            resolveOld(Response.json({ jobs: [deletion("error")] }));
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().completed).toBe(1),
            );
            expect(polls).toBe(3);
            expect(statuses).not.toContain("error");
        } finally {
            manager.setEnabled(false);
            window.removeEventListener("tgdrive:files-changed", changed);
        }
    });

    it("keeps active deletion visible with a connection warning until polling recovers", async () => {
        const fetcher = vi
            .fn()
            .mockRejectedValueOnce(new Error("offline"))
            .mockResolvedValueOnce(
                Response.json({ jobs: [deletion("completed")] }),
            );
        vi.stubGlobal("fetch", fetcher);
        const manager = managerForTest();
        try {
            manager.trackDeletions([{ id: "deletion" }]);
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().error).toContain(
                    "Retrying automatically",
                ),
            );
            expect(manager.deletionSnapshot().jobs[0].status).toBe("queued");
            manager.refreshJobs();
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().completed).toBe(1),
            );
            expect(manager.deletionSnapshot().error).toBe("");
        } finally {
            manager.setEnabled(false);
        }
    });
});

describe("safe resume and browser persistence", () => {
    it("checks all uploaded chunks before allowing a same-sized reselected file", async () => {
        const file = new File(["original"], "name.txt");
        const chunks = [
            { offset: 0, size: 4, sha256: await hash("orig") },
            { offset: 4, size: 4, sha256: await hash("inal") },
        ];
        await expect(
            verifyReceivedChunks(file, chunks, 8),
        ).resolves.toBeUndefined();
        await expect(
            verifyReceivedChunks(new File(["changed!"], "name.txt"), chunks, 8),
        ).rejects.toThrow("differs");
    });
    it("rejects missing and non-contiguous resume checksums", async () => {
        const file = new File(["abcd"], "a");
        await expect(verifyReceivedChunks(file, [], 4)).rejects.toThrow(
            "incomplete",
        );
        await expect(
            verifyReceivedChunks(
                file,
                [{ offset: 1, size: 3, sha256: await hash("bcd") }],
                4,
            ),
        ).rejects.toThrow("incomplete");
    });
    it("persists tracking metadata without source files, abort controllers or arbitrary error strings", () => {
        const transfer = {
            key: "id",
            name: "file.txt",
            size: 4,
            lastModified: 1,
            destination: "/",
            state: "error",
            phase: "Paused",
            current: 0,
            total: 4,
            file: new File(["test"], "file.txt"),
            controller: new AbortController(),
            error: "https://private.example/?credential=secret",
            password: "secret",
            auth: "token",
            url: "https://secret/",
        } as Transfer;
        const saved = JSON.stringify(safeTransferMetadata([transfer]));
        expect(saved).not.toContain("secret");
        expect(saved).not.toContain("token");
        expect(saved).not.toContain("controller");
        expect(saved).not.toContain('file":');
    });
    it("recovers unfinished browser receipt as a source-reselection action", () => {
        localStorage.setItem(
            "tgdrive.uploads.v2",
            JSON.stringify([
                {
                    key: "key",
                    initiatedHere: true,
                    name: "file",
                    size: 4,
                    lastModified: 1,
                    destination: "/",
                    state: "receiving",
                    phase: "Receiving",
                    current: 2,
                    total: 4,
                    uploadId: "upload",
                },
            ]),
        );
        const manager = managerForTest();
        expect(manager.snapshot()[0].state).toBe("awaiting_file");
        expect(manager.snapshot()[0].initiatedHere).toBe(true);
    });
    it("keeps URL jobs server-owned across a browser reload", async () => {
        const manager = managerForTest();
        manager.addJob("url-server-job", "URL file", "/", "remote");
        const restored = managerForTest();
        expect(restored.snapshot()[0]).toMatchObject({
            jobId: "url-server-job",
            kind: "remote",
            state: "uploading",
            initiatedHere: true,
        });
        await expect(
            restored.resume(
                restored.snapshot()[0].key,
                new File(["data"], "URL file"),
            ),
        ).rejects.toThrow("resume on the server");
    });
    it("never requests a local original for a URL receipt missing its server job", async () => {
        localStorage.setItem(
            "tgdrive.uploads.v2",
            JSON.stringify([
                {
                    key: "missing",
                    initiatedHere: true,
                    name: "URL file",
                    size: 4,
                    lastModified: 0,
                    destination: "/",
                    kind: "remote",
                    state: "uploading",
                    phase: "Sending",
                    current: 1,
                    total: 4,
                },
            ]),
        );
        const restored = managerForTest();
        expect(restored.snapshot()[0].state).toBe("error");
        expect(restored.snapshot()[0].phase).not.toContain("original");
        await expect(restored.retry("missing")).rejects.toThrow(
            "no server job",
        );
    });
    it("does not turn globally discovered transfers into local popup receipts", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "another-device",
                            kind: "upload",
                            status: "running",
                            phase: "uploading",
                            current: 1,
                            total: 4,
                        },
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(1));
            expect(manager.snapshot()[0].initiatedHere).not.toBe(true);
            const restored = managerForTest();
            // The server is authoritative for discovered rows; stale browser copies
            // must not resurrect a job removed by a fixture/drive reset.
            expect(restored.snapshot()).toEqual([]);
            try {
                restored.setEnabled(true);
                await vi.waitFor(() =>
                    expect(restored.snapshot()).toHaveLength(1),
                );
                expect(restored.snapshot()[0].initiatedHere).not.toBe(true);
                expect(restored.snapshot()[0].jobId).toBe("another-device");
            } finally {
                restored.setEnabled(false);
            }
        } finally {
            manager.setEnabled(false);
        }
    });
});
describe("resumable HTTP upload protocol", () => {
    it("publishes in-flight device bytes without persisting or resuming unconfirmed bytes", async () => {
        let report: ((bytes: number) => void) | undefined;
        let receipt: ((response: Response) => void) | undefined;
        const offsets: string[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input.endsWith("/complete"))
                    return Response.json({ id: "job" });
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "8" },
                    });
                return Response.json({
                    id: "u",
                    offset: 0,
                    total_size: 8,
                    chunk_size: 8,
                    checksums: [],
                });
            }),
        );
        const manager = new TransferManager(async (_url, options, progress) => {
            offsets.push(new Headers(options.headers).get("Upload-Offset")!);
            report = progress;
            return new Promise<Response>((resolve) => {
                receipt = resolve;
            });
        });
        try {
            manager.setEnabled(true);
            manager.add([new File(["abcdefgh"], "source.txt")], "/", 10);
            await vi.waitFor(() => expect(report).toBeDefined());
            const writes = vi.spyOn(localStorage, "setItem");
            const scans = vi.spyOn(localStorage, "key");
            report!(3);
            expect(manager.snapshot()[0]).toMatchObject({
                current: 0,
                sentCurrent: 3,
                state: "receiving",
            });
            report!(6);
            expect(manager.snapshot()[0].sentCurrent).toBe(6);
            expect(writes).not.toHaveBeenCalled();
            expect(scans).not.toHaveBeenCalled();
            writes.mockRestore();
            scans.mockRestore();
            const saved = JSON.parse(
                localStorage.getItem(
                    "tgdrive.uploads.v2:" + manager.snapshot()[0].key,
                )!,
            );
            expect(saved.current).toBe(0);
            expect(saved.sentCurrent).toBeUndefined();
            receipt!(new Response(null, { status: 409 }));
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].state).toBe("error"),
            );
            expect(manager.snapshot()[0].sentCurrent).toBeUndefined();
            const key = manager.snapshot()[0].key;
            report = undefined;
            await manager.retry(key);
            await vi.waitFor(() => expect(report).toBeDefined());
            expect(offsets).toEqual(["0", "0"]);
            expect(manager.snapshot()[0]).toMatchObject({
                current: 0,
                sentCurrent: 0,
            });
            report!(8);
            expect(manager.snapshot()[0].phase).toContain(
                "Waiting for server confirmation",
            );
            receipt!(
                new Response(null, {
                    status: 204,
                    headers: { "Upload-Offset": "8" },
                }),
            );
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].jobId).toBe("job"),
            );
            expect(manager.snapshot()[0].sentCurrent).toBeUndefined();
        } finally {
            manager.setEnabled(false);
        }
    });
    it("sends each chunk at its acknowledged offset, then tracks the independent Telegram job", async () => {
        const calls: {
            url: string;
            method: string;
            offset: string | null;
            size: number;
        }[] = [];
        let offset = 0;
        let complete = false;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                const method = options.method || "GET";
                const headers = new Headers(options.headers);
                calls.push({
                    url: input,
                    method,
                    offset: headers.get("Upload-Offset"),
                    size: options.body instanceof Blob ? options.body.size : 0,
                });
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (method === "POST" && input === "/api/v1/uploads")
                    return Response.json({
                        id: "upload",
                        offset: 0,
                        total_size: 8,
                        chunk_size: 4,
                    });
                if (method === "HEAD")
                    return new Response(null, {
                        headers: {
                            "Upload-Offset": String(offset),
                            "Upload-Length": "8",
                        },
                    });
                if (method === "PATCH") {
                    expect(Number(headers.get("Upload-Offset"))).toBe(offset);
                    offset += (options.body as Blob).size;
                    return new Response(null, {
                        status: 204,
                        headers: { "Upload-Offset": String(offset) },
                    });
                }
                if (input.endsWith("/complete")) {
                    complete = true;
                    return Response.json({ id: "job" });
                }
                return Response.json({
                    id: "upload",
                    offset,
                    total_size: 8,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            manager.add([new File(["abcdefgh"], "source.txt")], "/", 1024);
            await vi.waitFor(() => expect(complete).toBe(true));
            expect(
                calls
                    .filter((call) => call.method === "PATCH")
                    .map((call) => [call.offset, call.size]),
            ).toEqual([
                ["0", 4],
                ["4", 4],
            ]);
            expect(manager.snapshot()[0].jobId).toBe("job");
            expect(manager.snapshot()[0].phase).toBe("Queued for Telegram");
            expect(calls.some((call) => call.method === "HEAD")).toBe(false);
            expect(
                calls.some(
                    (call) =>
                        call.method === "GET" &&
                        call.url === "/api/v1/uploads/upload",
                ),
            ).toBe(false);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("does not complete a file when the server fails to acknowledge the chunk", async () => {
        let completed = false;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (options.method === "POST" && input === "/api/v1/uploads")
                    return Response.json({
                        id: "u",
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                if (options.method === "PATCH")
                    return new Response(null, { status: 204 });
                if (input.endsWith("/complete")) completed = true;
                return Response.json({
                    id: "u",
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            manager.add([new File(["abcd"], "a")], "/", 10);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].state).toBe("error"),
            );
            expect(completed).toBe(false);
        } finally {
            manager.setEnabled(false);
        }
    });
});
describe("opaque paths and Unicode filenames", () => {
    it("joins nested IDs without exposing filenames as paths", () =>
        expect(
            itemPath({
                id: "file",
                path: "/folder/",
                name: "資料 #1.txt",
                type: "file",
            }),
        ).toBe("/folder/file"));
    it("encodes share credentials and paths as query data", () => {
        const url = new URL(
            fileUrl("/folder/id", "signed+/token"),
            "http://test",
        );
        expect(url.searchParams.get("auth")).toBe("signed+/token");
        expect(url.searchParams.get("path")).toBe("/folder/id");
    });
    it("formats zero bytes honestly", () => expect(formatBytes(0)).toBe("0 B"));
});

describe("ambiguous completion and verification cancellation", () => {
    it("reattaches a server-accepted upload after its complete response is lost", async () => {
        let accepted = false;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({
                        jobs: accepted
                            ? [
                                  {
                                      id: "same-id",
                                      status: "running",
                                      phase: "uploading",
                                      current: 0,
                                      total: 4,
                                      name: "original.bin",
                                      destination: "/",
                                  },
                              ]
                            : [],
                    });
                if (options.method === "POST" && input === "/api/v1/uploads")
                    return Response.json({
                        id: "same-id",
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                if (options.method === "PATCH")
                    return new Response(null, {
                        status: 204,
                        headers: { "Upload-Offset": "4" },
                    });
                if (input.endsWith("/complete")) {
                    accepted = true;
                    throw new TypeError("network interrupted after acceptance");
                }
                return Response.json({
                    id: "same-id",
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            manager.add([new File(["abcd"], "original.bin")], "/", 10);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0]).toMatchObject({
                    state: "queued",
                    stage: "retry_wait",
                }),
            );
            manager.setEnabled(false);
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].jobId).toBe("same-id"),
            );
            expect(manager.snapshot()).toHaveLength(1);
            expect(manager.snapshot()[0].state).toBe("uploading");
        } finally {
            manager.setEnabled(false);
        }
    });
    it("stops original-file verification when cancellation is requested", async () => {
        const controller = new AbortController();
        controller.abort();
        await expect(
            verifyReceivedChunks(
                new File(["abcd"], "source"),
                [{ offset: 0, size: 4, sha256: await hash("abcd") }],
                4,
                () => {},
                controller.signal,
            ),
        ).rejects.toMatchObject({ name: "AbortError" });
    });
});

it("shows the server's actionable chunk error without persisting its details", async () => {
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: string, options: RequestInit = {}) => {
            if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
            if (options.method === "POST" && input === "/api/v1/uploads")
                return Response.json({
                    id: "quota",
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                });
            if (options.method === "HEAD")
                return new Response(null, {
                    headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                });
            if (options.method === "PATCH")
                return Response.json(
                    {
                        detail: "Temporary Storage is full. Free disk space before retrying.",
                    },
                    { status: 507 },
                );
            return Response.json({
                id: "quota",
                offset: 0,
                total_size: 4,
                chunk_size: 4,
                checksums: [],
            });
        }),
    );
    const manager = managerForTest();
    try {
        manager.setEnabled(true);
        manager.add([new File(["abcd"], "quota.bin")], "/", 10);
        await vi.waitFor(() =>
            expect(manager.snapshot()[0].phase).toBe(
                "Waiting for server space",
            ),
        );
        expect(manager.snapshot()[0].state).toBe("queued");
        expect(JSON.stringify(managerForTest().snapshot())).not.toContain(
            "Free disk space",
        );
    } finally {
        manager.setEnabled(false);
    }
});

it("opens the demo protection notice when a resumed upload chunk becomes read-only", async () => {
    const refused = vi.fn();
    window.addEventListener("tgdrive:demo-files-read-only", refused);
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: string, options: RequestInit = {}) => {
            if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
            if (options.method === "HEAD")
                return new Response(null, {
                    headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                });
            if (options.method === "PATCH")
                return Response.json(
                    {
                        code: "DEMO_FILES_READ_ONLY",
                        detail: "Demo Files is read-only.",
                    },
                    { status: 403 },
                );
            return Response.json({
                id: "protected-upload",
                offset: 0,
                total_size: 4,
                chunk_size: 4,
                checksums: [],
            });
        }),
    );
    const manager = managerForTest();
    try {
        manager.setEnabled(true);
        manager.add(
            [new File(["abcd"], "demo.bin")],
            "/tgdrive-demo-files",
            10,
        );
        await vi.waitFor(() =>
            expect(manager.snapshot()[0].state).toBe("error"),
        );
        expect(refused).toHaveBeenCalledOnce();
        expect(manager.snapshot()[0].error).toBe("Demo Files is read-only.");
    } finally {
        manager.setEnabled(false);
    }
});

describe("whole-batch recovery and bounded admission", () => {
    it("gives every ordinary file a stable task identity before and after reload", () => {
        const manager = managerForTest();
        manager.add(
            [new File(["one"], "first.bin"), new File(["two"], "second.bin")],
            "/",
            10,
        );
        const original = manager.snapshot();
        expect(original.map((item) => item.batchName)).toEqual([
            "first.bin",
            "second.bin",
        ]);
        expect(new Set(original.map(transferTaskId)).size).toBe(2);
        const restored = managerForTest().snapshot();
        expect(restored.map(transferTaskId)).toEqual(
            original.map(transferTaskId),
        );
        expect(restored.every((item) => !item.folderUpload)).toBe(true);
    });
    it("persists explicit folder task metadata and reads the same identity from server jobs", () => {
        const folderUpload = {
            id: "folder-upload-identity",
            name: "Folder",
            root_path: "/root",
            created_at: 1000,
        };
        const manager = managerForTest();
        manager.addEntries(
            [
                { file: new File(["one"], "first.bin"), destination: "/root" },
                { file: new File(["two"], "second.bin"), destination: "/root" },
            ],
            10,
            true,
            { name: "Folder", folderUpload },
        );
        const restored = managerForTest().snapshot();
        expect(
            restored.every((item) => item.folderUpload?.id === folderUpload.id),
        ).toBe(true);
        expect(new Set(restored.map(transferTaskId))).toEqual(
            new Set([folderUpload.id]),
        );
        const job = fromJob({
            id: "server-member",
            status: "queued",
            phase: "queued",
            current: 0,
            total: 3,
            folder_upload: folderUpload,
        });
        expect(job).toMatchObject({
            folderUpload,
            batchId: folderUpload.id,
            batchName: folderUpload.name,
            batchCreatedAt: 1_000_000,
        });
        expect(
            fromJob({
                id: "standalone-member",
                status: "queued",
                phase: "queued",
                current: 0,
                total: 3,
            }),
        ).not.toHaveProperty("folderUpload");
    });
    it("retains completed siblings in an unfinished batch beyond the old hundred-row limit", () => {
        const manager = managerForTest();
        manager.addEntries(
            Array.from({ length: 150 }, (_, i) => ({
                file: new File(["x"], "file-" + i),
                relativePath: "Folder/file-" + i,
                destination: "/",
            })),
            10,
            true,
            folderBatch("Folder"),
        );
        const original = manager.snapshot();
        for (const item of original.slice(0, -1)) {
            item.state = "completed";
            item.finishedAt = Date.now();
        }
        manager.clearFinished();
        expect(manager.snapshot()).toHaveLength(150);
        const reloaded = managerForTest().snapshot();
        expect(reloaded).toHaveLength(150);
        expect(
            reloaded.filter((item) => item.state === "completed"),
        ).toHaveLength(149);
        expect(reloaded[149].state).toBe("awaiting_file");
        expect(new Set(reloaded.map((item) => item.batchId)).size).toBe(1);
    });
    it("keeps history as whole batches and retains errors", () => {
        const rows = Array.from({ length: 31 }, (_, batch) =>
            Array.from(
                { length: 2 },
                (_, index) =>
                    ({
                        key: batch + "-" + index,
                        batchId: "batch-" + batch,
                        folderUpload: {
                            id: "batch-" + batch,
                            name: "Folder",
                            root_path: "/folder",
                            created_at: 1,
                        },
                        relativePath: "Folder/file-" + index,
                        name: "file",
                        destination: "/",
                        size: 1,
                        lastModified: 1,
                        state: "completed",
                        phase: "Done",
                        current: 1,
                        total: 1,
                        finishedAt: batch + 1,
                    }) as Transfer,
            ),
        ).flat();
        const failed = {
            ...rows[0],
            key: "failed",
            batchId: "failed",
            folderUpload: undefined,
            state: "error" as const,
        };
        const kept = retainTransferBatches([...rows, failed]);
        expect(kept).toHaveLength(61);
        expect(kept.some((item) => item.batchId === "batch-0")).toBe(false);
        expect(kept.some((item) => item.key === "failed")).toBe(true);
        for (let batch = 1; batch < 31; batch++)
            expect(
                kept.filter((item) => item.batchId === "batch-" + batch),
            ).toHaveLength(2);
    });
    it("caps unfinished-batch rows at 2000 including retained completed siblings", () => {
        const manager = managerForTest();
        manager.addEntries(
            Array.from({ length: 2000 }, (_, i) => ({
                file: new File(["x"], "" + i),
                relativePath: "Folder/" + i,
                destination: "/",
            })),
            10,
            true,
            folderBatch("Folder"),
        );
        manager.snapshot()[0].state = "completed";
        expect(manager.canQueue(1)).toBe(false);
        expect(manager.add([new File(["x"], "extra")], "/", 10)[0]).toContain(
            "2,000",
        );
        expect(manager.snapshot()).toHaveLength(2000);
    });
    it("matches folder resume by exact rooted path without flattening duplicate basenames", async () => {
        const file = (path: string, data = "same") => {
            const file = new File([data], path.split("/").at(-1)!);
            Object.defineProperty(file, "webkitRelativePath", { value: path });
            return file;
        };
        const manager = managerForTest();
        manager.addEntries(
            [
                {
                    file: file("Root/a/notes.txt"),
                    relativePath: "Root/a/notes.txt",
                    destination: "/r/a",
                },
                {
                    file: file("Root/b/notes.txt"),
                    relativePath: "Root/b/notes.txt",
                    destination: "/r/b",
                },
            ],
            10,
            true,
            folderBatch("Root", "/r"),
        );
        const reloaded = managerForTest();
        const id = reloaded.snapshot()[0].batchId!;
        const result = await reloaded.resumeBatchFiles(id, [
            file("Root/b/notes.txt"),
        ]);
        expect(result).toMatchObject({ matched: 1, remaining: 1 });
        expect(reloaded.snapshot()[0].state).toBe("awaiting_file");
        expect(reloaded.snapshot()[1].state).toBe("queued");
        expect(reloaded.snapshot()[1].destination).toBe("/r/b");
        expect(
            new Set(reloaded.snapshot().map((item) => item.batchId)).size,
        ).toBe(1);
    });
    it("resumes only the selected ordinary task when file signatures are identical", async () => {
        const manager = managerForTest();
        const source = new File(["same"], "same.txt", { lastModified: 42 });
        manager.add([source, source], "/", 10);
        const reloaded = managerForTest();
        const result = await reloaded.resumeBatchFiles(
            reloaded.snapshot()[0].batchId!,
            [source],
        );
        expect(result.matched).toBe(1);
        expect(result.remaining).toBe(0);
        expect(result.errors).toEqual([]);
        expect(reloaded.snapshot()[0].state).toBe("queued");
        expect(reloaded.snapshot()[1].state).toBe("awaiting_file");
    });
    it("skips recognized folder members that no longer need their originals", async () => {
        const file = (name: string) => {
            const source = new File(["same"], name);
            Object.defineProperty(source, "webkitRelativePath", {
                value: "Root/" + name,
            });
            return source;
        };
        const states = [
            "awaiting_file",
            "queued",
            "receiving",
            "verifying",
            "uploading",
            "completed",
            "cancelled",
            "error",
        ] as const;
        const files = states.map((state, index) => file(state + index));
        const manager = managerForTest();
        const folder = folderBatch("Root");
        manager.addEntries(
            files.map((source) => ({
                file: source,
                relativePath: source.webkitRelativePath,
                destination: "/Root",
            })),
            10,
            true,
            folder,
        );
        const members = manager.snapshot();
        members.forEach((item, index) => {
            item.state = states[index];
            if (index === 0) item.file = undefined;
            if ([4, 5, 7].includes(index)) {
                item.jobId = "job-" + index;
                item.file = undefined;
            }
        });
        manager.addEntries(
            [
                {
                    file: files[0],
                    relativePath: "Root/" + files[0].name,
                    destination: "/other",
                },
            ],
            10,
            true,
            folderBatch("Root", "/other"),
        );
        const other = manager.snapshot().at(-1)!;
        other.state = "awaiting_file";
        other.file = undefined;
        const untouched = manager
            .snapshot()
            .slice(1)
            .map((item) => ({ ...item }));

        expect(
            await manager.resumeBatchFiles(folder.folderUpload.id, files),
        ).toEqual({
            matched: 1,
            remaining: 0,
            skipped: 7,
            unmatched: 0,
            errors: [],
        });
        expect(manager.snapshot()[0]).toMatchObject({
            state: "queued",
            file: files[0],
        });
        expect(manager.snapshot().slice(1)).toEqual(untouched);
        expect(
            await manager.resumeBatchFiles(folder.folderUpload.id, files),
        ).toEqual({
            matched: 0,
            remaining: 0,
            skipped: 8,
            unmatched: 0,
            errors: [],
        });
        expect(manager.snapshot().slice(1)).toEqual(untouched);
    });
    it("still reports unrelated and changed-size originals as unmatched", async () => {
        const file = (path: string, data = "same") => {
            const source = new File([data], path.split("/").at(-1)!);
            Object.defineProperty(source, "webkitRelativePath", {
                value: path,
            });
            return source;
        };
        const manager = managerForTest();
        const folder = folderBatch("Root");
        const originals = [
            file("Root/pending"),
            file("Root/changed"),
            file("Root/completed"),
        ];
        manager.addEntries(
            originals.map((source) => ({
                file: source,
                relativePath: source.webkitRelativePath,
                destination: "/Root",
            })),
            10,
            true,
            folder,
        );
        const members = manager.snapshot();
        members[0].state = "awaiting_file";
        members[0].file = undefined;
        members[1].state = "awaiting_file";
        members[1].file = undefined;
        members[2].state = "completed";
        members[2].jobId = "completed-job";
        members[2].file = undefined;

        const result = await manager.resumeBatchFiles(folder.folderUpload.id, [
            originals[0],
            file("Root/changed", "different size"),
            file("Root/completed", "different size"),
            file("Other/pending"),
        ]);
        expect(result).toMatchObject({
            matched: 1,
            remaining: 1,
            skipped: 0,
            unmatched: 3,
        });
        expect(manager.snapshot().map((item) => item.state)).toEqual([
            "queued",
            "awaiting_file",
            "completed",
        ]);
    });
});

describe("independent browser receiving and Telegram backlog", () => {
    it("drains receipt handoffs after failures without stranding later files", async () => {
        const handoffs: string[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    const body = JSON.parse(String(options.body));
                    return Response.json({
                        id: body.name,
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                }
                if (input.endsWith("/complete")) {
                    const id = input.split("/")[4];
                    handoffs.push(id);
                    return id === "file-0" || id === "file-1"
                        ? Response.json(
                              { detail: "Receipt verification unavailable" },
                              { status: 503 },
                          )
                        : Response.json({ id });
                }
                throw new Error("Unexpected request: " + input);
            }),
        );
        const manager = new TransferManager(
            async () =>
                new Response(null, {
                    status: 204,
                    headers: { "Upload-Offset": "4" },
                }),
        );
        try {
            manager.add(
                Array.from(
                    { length: 6 },
                    (_, i) => new File(["data"], "file-" + i),
                ),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(handoffs).toHaveLength(6));
            expect(
                manager
                    .snapshot()
                    .filter(
                        (item) =>
                            item.state === "queued" &&
                            item.stage === "retry_wait",
                    ),
            ).toHaveLength(2);
            expect(
                manager
                    .snapshot()
                    .slice(2)
                    .every((item) => item.jobId && !item.controller),
            ).toBe(true);
            expect(
                manager
                    .snapshot()
                    .slice(0, 2)
                    .every(
                        (item) => item.file && !item.jobId && !item.controller,
                    ),
            ).toBe(true);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("pauses a late-created receipt on logout and safely resumes it after login", async () => {
        let created!: (response: Response) => void;
        let creations = 0;
        let cancellations = 0;
        let checks = 0;
        const snapshot = {
            id: "receipt",
            offset: 0,
            total_size: 4,
            chunk_size: 4,
        };
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    creations += 1;
                    return new Promise<Response>((resolve) => {
                        created = resolve;
                    });
                }
                if (input.endsWith("/cancel")) {
                    cancellations += 1;
                    return Response.json({});
                }
                if (input.endsWith("/complete"))
                    return Response.json({ id: "receipt" });
                if (options.method === "HEAD") {
                    checks += 1;
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                }
                return Response.json(snapshot);
            }),
        );
        const send = vi.fn(
            async () =>
                new Response(null, {
                    status: 204,
                    headers: { "Upload-Offset": "4" },
                }),
        );
        const manager = new TransferManager(send);
        const file = new File(["data"], "file");
        try {
            manager.setEnabled(true);
            manager.add([file], "/", 10);
            await vi.waitFor(() => expect(created).toBeDefined());
            manager.setEnabled(false);
            created(Response.json(snapshot));
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].state).toBe("awaiting_file"),
            );
            expect(cancellations).toBe(0);
            expect(send).not.toHaveBeenCalled();
            manager.setEnabled(true);
            await manager.resume(manager.snapshot()[0].key, file);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].jobId).toBe("receipt"),
            );
            expect(creations).toBe(1);
            expect(checks).toBe(1);
            expect(send).toHaveBeenCalledOnce();
        } finally {
            manager.setEnabled(false);
        }
    });
    it("keeps a late-created receipt until its cancellation is confirmed", async () => {
        let created!: (response: Response) => void;
        let cancellations = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads")
                    return new Promise<Response>((resolve) => {
                        created = resolve;
                    });
                if (input.endsWith("/cancel")) {
                    cancellations += 1;
                    return Response.json({});
                }
                throw new Error("Unexpected request: " + input);
            }),
        );
        const send = vi.fn();
        const manager = new TransferManager(send);
        try {
            manager.setEnabled(true);
            manager.add([new File(["data"], "file")], "/", 10);
            await vi.waitFor(() => expect(created).toBeDefined());
            const cancelling = manager.cancel(manager.snapshot()[0].key);
            manager.clearFinished();
            expect(manager.snapshot()[0].state).toBe("cancelling");
            created(
                Response.json({
                    id: "late",
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                }),
            );
            await cancelling;
            await vi.waitFor(() => expect(cancellations).toBe(1));
            expect(send).not.toHaveBeenCalled();
            manager.clearFinished();
            expect(manager.snapshot()).toHaveLength(0);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("applies Retry-After to queued receipt handoffs without stopping active payloads", async () => {
        vi.useFakeTimers();
        const handoffs: string[] = [];
        const patched: string[] = [];
        const finishers: ((response: Response) => void)[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    const body = JSON.parse(String(options.body));
                    return Response.json({
                        id: body.name,
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                }
                const id = input.split("/")[4];
                if (input.endsWith("/complete")) {
                    handoffs.push(id);
                    if (handoffs.length === 1)
                        return Response.json(
                            { detail: "Admission busy" },
                            { status: 429, headers: { "Retry-After": "10" } },
                        );
                    return new Promise<Response>((resolve) =>
                        finishers.push(resolve),
                    );
                }
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                return Response.json({
                    id,
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                });
            }),
        );
        const manager = new TransferManager(async (url) => {
            patched.push(url.split("/")[4]);
            return new Response(null, {
                status: 204,
                headers: { "Upload-Offset": "4" },
            });
        });
        try {
            manager.add(
                Array.from(
                    { length: 5 },
                    (_, i) => new File(["data"], "file-" + i),
                ),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.advanceTimersByTimeAsync(0);
            expect(handoffs).toHaveLength(2);
            expect(patched.length).toBeGreaterThan(handoffs.length);
            await vi.advanceTimersByTimeAsync(9999);
            expect(handoffs).toHaveLength(2);
            await vi.advanceTimersByTimeAsync(1);
            expect(handoffs).toHaveLength(3);
        } finally {
            manager.setEnabled(false);
            for (const finish of finishers)
                finish(Response.json({ id: "accepted" }));
            await vi.advanceTimersByTimeAsync(0);
            vi.useRealTimers();
        }
    });
    it("keeps device bytes flowing through a bounded, slow receipt handoff", async () => {
        const patched: string[] = [];
        const finishers = new Map<string, (response: Response) => void>();
        let completing = 0;
        let maximumCompleting = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    const body = JSON.parse(String(options.body));
                    return Response.json({
                        id: body.name,
                        offset: 0,
                        total_size: body.total_size,
                        chunk_size: 4,
                        checksums: [],
                    });
                }
                const id = input.split("/")[4];
                if (input.endsWith("/cancel")) return Response.json({});
                if (input.endsWith("/complete")) {
                    completing += 1;
                    maximumCompleting = Math.max(maximumCompleting, completing);
                    return new Promise<Response>((resolve) => {
                        finishers.set(id, (response) => {
                            completing -= 1;
                            resolve(response);
                        });
                    });
                }
                throw new Error("Unexpected receipt check: " + input);
            }),
        );
        const manager = new TransferManager(async (url) => {
            patched.push(url.split("/")[4]);
            return new Response(null, {
                status: 204,
                headers: { "Upload-Offset": "4" },
            });
        });
        try {
            manager.add(
                Array.from(
                    { length: 7 },
                    (_, i) => new File(["data"], "file-" + i),
                ),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(patched).toHaveLength(7));
            expect(finishers.size).toBe(2);
            expect(maximumCompleting).toBe(2);
            expect(manager.snapshot().every((item) => item.current === 4)).toBe(
                true,
            );
            // Received bytes alone cannot declare the tab safe to close.
            expect(
                manager.snapshot().every((item) => !item.jobId && item.file),
            ).toBe(true);
            const cancelled = manager.snapshot()[4];
            await manager.cancel(cancelled.key);
            expect(cancelled.state).toBe("cancelled");
            // Release each admitted handoff; queued cancellations must never enter it.
            for (const id of [
                "file-0",
                "file-1",
                "file-2",
                "file-3",
                "file-5",
                "file-6",
            ]) {
                await vi.waitFor(() => expect(finishers.has(id)).toBe(true));
                finishers.get(id)!(Response.json({ id }));
            }
            await vi.waitFor(() =>
                expect(
                    manager.snapshot().filter((item) => item.jobId),
                ).toHaveLength(6),
            );
            expect(finishers.has("file-4")).toBe(false);
            expect(maximumCompleting).toBe(2);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("gives a newly added folder the next device slot before an older folder's backlog", async () => {
        const admitted: string[] = [];
        const patches = new Map<string, (response: Response) => void>();
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    const body = JSON.parse(String(options.body));
                    admitted.push(body.name);
                    return Response.json({
                        id: body.name,
                        offset: 0,
                        total_size: body.total_size,
                        chunk_size: 4,
                    });
                }
                if (input.endsWith("/complete"))
                    return Response.json({ id: input.split("/")[4] });
                throw new Error("Unexpected receipt check: " + input);
            }),
        );
        const manager = new TransferManager(
            (url, options) =>
                new Promise<Response>((resolve, reject) => {
                    patches.set(url.split("/")[4], resolve);
                    options.signal.addEventListener("abort", () =>
                        reject(new DOMException("Upload paused", "AbortError")),
                    );
                }),
        );
        const entries = (prefix: string, count: number) =>
            Array.from({ length: count }, (_, i) => ({
                file: new File(["data"], prefix + i),
                destination: "/" + prefix,
                relativePath: prefix + "/" + i,
            }));
        const acknowledge = (id: string) =>
            patches.get(id)!(
                new Response(null, {
                    status: 204,
                    headers: { "Upload-Offset": "4" },
                }),
            );
        try {
            manager.addEntries(
                entries("old", 5),
                10,
                true,
                folderBatch("Old", "/old"),
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(patches.size).toBe(2));
            manager.addEntries(
                entries("new", 2),
                10,
                true,
                folderBatch("New", "/new"),
            );
            acknowledge("old0");
            await vi.waitFor(() => expect(admitted).toHaveLength(3));
            expect(admitted).toEqual(["old0", "old1", "new0"]);
            await vi.waitFor(() => expect(patches.has("new0")).toBe(true));
            acknowledge("new0");
            await vi.waitFor(() => expect(admitted).toHaveLength(4));
            expect(admitted[3]).toBe("old2");
            acknowledge("old1");
            await vi.waitFor(() => expect(admitted).toHaveLength(5));
            expect(admitted[4]).toBe("new1");
        } finally {
            manager.setEnabled(false);
        }
    });
    it("notifies the listing once per poll when files become available before backup finishes", async () => {
        const changed = vi.fn();
        window.addEventListener("tgdrive:files-changed", changed);
        const jobs = Array.from({ length: 3 }, (_, index) => ({
            id: "visible-" + index,
            kind: "upload",
            status: "running",
            phase: "backup_pending",
            current: 4,
            total: 4,
        }));
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ jobs })),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(3));
            expect(changed).toHaveBeenCalledTimes(1);
            for (const job of jobs) job.status = "completed";
            manager.refreshJobs();
            await vi.waitFor(() =>
                expect(
                    manager
                        .snapshot()
                        .every((item) => item.state === "completed"),
                ).toBe(true),
            );
            expect(changed).toHaveBeenCalledTimes(1);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("admits smaller files while larger files wait for staging space", async () => {
        const complete: string[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json({ jobs: [] });
                if (input === "/api/v1/uploads") {
                    const body = JSON.parse(String(options.body));
                    if (body.name.startsWith("large"))
                        return Response.json(
                            { detail: "Not enough space" },
                            { status: 507 },
                        );
                    return Response.json({
                        id: body.name,
                        offset: 0,
                        total_size: body.total_size,
                        chunk_size: 4,
                        checksums: [],
                    });
                }
                const id = input.split("/")[4];
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                if (options.method === "PATCH")
                    return new Response(null, {
                        status: 204,
                        headers: { "Upload-Offset": "4" },
                    });
                if (input.endsWith("/complete")) {
                    complete.push(id);
                    return Response.json({ id });
                }
                return Response.json({
                    id,
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.add(
                [
                    ...Array.from(
                        { length: 4 },
                        (_, i) => new File(["large-data"], "large-" + i),
                    ),
                    new File(["data"], "small"),
                ],
                "/",
                100,
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(complete).toEqual(["small"]));
            expect(
                manager
                    .snapshot()
                    .slice(0, 4)
                    .every((item) => item.state === "queued"),
            ).toBe(true);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("keeps sending around a slow file even when progress polling is unavailable", async () => {
        let receipts = 0;
        const completed: string[] = [];
        let release!: () => void;
        const slow = new Promise<void>((resolve) => {
            release = resolve;
        });
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs")
                    return Response.json(
                        { detail: "Progress unavailable" },
                        { status: 503 },
                    );
                if (input === "/api/v1/uploads")
                    return Response.json({
                        id: "receipt-" + ++receipts,
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                        checksums: [],
                    });
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                const id = input.split("/")[4];
                if (input.endsWith("/complete")) {
                    completed.push(id);
                    return Response.json({ id });
                }
                return Response.json({
                    id,
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = new TransferManager(async (url) => {
            if (url.endsWith("receipt-1")) await slow;
            return new Response(null, {
                status: 204,
                headers: { "Upload-Offset": "4" },
            });
        });
        try {
            manager.add(
                Array.from(
                    { length: 7 },
                    (_, i) => new File(["data"], "file-" + i),
                ),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(completed).toHaveLength(6));
            expect(receipts).toBe(7);
            expect(manager.snapshot()[0].state).toBe("receiving");
            expect(
                manager
                    .snapshot()
                    .slice(1)
                    .every((item) => item.jobId),
            ).toBe(true);
            release();
            await vi.waitFor(() => expect(completed).toHaveLength(7));
        } finally {
            release();
            manager.setEnabled(false);
        }
    });
    it("receives all five files while earlier Telegram uploads remain busy", async () => {
        let receipts = 0;
        let complete = 0;
        let patching = 0;
        let maxPatching = 0;
        const jobs: {
            id: string;
            status: string;
            phase: string;
            current: number;
            total: number;
            kind: string;
        }[] = [];
        const offsets = new Map<string, number>();
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs") return Response.json({ jobs });
                if (input === "/api/v1/uploads" && options.method === "POST") {
                    const id = "upload-" + ++receipts;
                    offsets.set(id, 0);
                    return Response.json({
                        id,
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                }
                const id = input.split("/")[4];
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: {
                            "Upload-Offset": String(offsets.get(id)),
                            "Upload-Length": "4",
                        },
                    });
                if (options.method === "PATCH") {
                    patching += 1;
                    maxPatching = Math.max(maxPatching, patching);
                    await Promise.resolve();
                    offsets.set(id, 4);
                    patching -= 1;
                    return new Response(null, {
                        status: 204,
                        headers: { "Upload-Offset": "4" },
                    });
                }
                if (input.endsWith("/complete")) {
                    jobs.push({
                        id,
                        status: "running",
                        phase: "uploading",
                        current: 0,
                        total: 4,
                        kind: "upload",
                    });
                    complete += 1;
                    return Response.json({ id });
                }
                return Response.json({
                    id,
                    offset: offsets.get(id),
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.add(
                Array.from({ length: 5 }, (_, i) => new File(["data"], "" + i)),
                "/",
                10,
            );
            manager.setEnabled(true);
            await vi.waitFor(() => expect(complete).toBe(5));
            expect(receipts).toBe(5);
            expect(jobs.every((job) => job.status === "running")).toBe(true);
            expect(maxPatching).toBe(BROWSER_RECEIVE_INITIAL_CONCURRENCY);
        } finally {
            manager.setEnabled(false);
        }
    });
    it("preserves known URL origin when the server changes its job kind to upload", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "remote",
                            status: "running",
                            phase: "uploading",
                            current: 1,
                            total: 4,
                            kind: "upload",
                        },
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        manager.addJob("remote", "URL file", "/", "remote");
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0].serverStatus).toBe("running"),
            );
            expect(manager.snapshot()[0].kind).toBe("remote");
        } finally {
            manager.setEnabled(false);
        }
    });
});

it("warns when browser storage cannot retain recovery metadata while allowing uploads", () => {
    vi.stubGlobal("localStorage", {
        length: 0,
        getItem: () => null,
        setItem: () => {
            throw new DOMException("full", "QuotaExceededError");
        },
    });
    const manager = managerForTest();
    expect(manager.add([new File(["data"], "file")], "/", 10)).toEqual([]);
    expect(manager.snapshot()).toHaveLength(1);
    expect(manager.recoveryWarning).toContain("could not save");
});

it("continues device receipts while four recovered Telegram jobs are busy", async () => {
    let receipts = 0;
    const jobs = Array.from({ length: 4 }, (_, i) => ({
        id: "recovered-" + i,
        name: "server",
        destination: "/",
        status: "running",
        phase: "uploading",
        current: 0,
        total: 4,
        kind: "remote",
    }));
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: string) => {
            if (input === "/api/v1/jobs") return Response.json({ jobs });
            if (input === "/api/v1/uploads") receipts += 1;
            throw new Error("synthetic stopped receipt");
        }),
    );
    const manager = managerForTest();
    try {
        manager.add([new File(["data"], "new")], "/", 10);
        manager.setEnabled(true);
        await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(5));
        await vi.waitFor(() => expect(receipts).toBe(1));
    } finally {
        manager.setEnabled(false);
    }
});

describe("Telegram backup protection", () => {
    it("keeps checkpoint-pending files unfinished and refuses cancellation locally", async () => {
        const fetchMock = vi.fn(async () =>
            Response.json({
                jobs: [
                    {
                        id: "protected-job",
                        status: "running",
                        phase: "backup_pending",
                        name: "safe.txt",
                        current: 4,
                        total: 4,
                        kind: "upload",
                    },
                ],
            }),
        );
        vi.stubGlobal("fetch", fetchMock);
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0]?.stage).toBe("backup_pending"),
            );
            manager.setEnabled(false);
            fetchMock.mockClear();
            expect(manager.snapshot()[0].state).toBe("uploading");
            expect(manager.snapshot()[0].phase).toContain(
                "Securing your drive backup",
            );
            await manager.cancelAll();
            await manager.cancel(manager.snapshot()[0].key);
            expect(fetchMock).not.toHaveBeenCalled();
            expect(manager.snapshot()[0].state).toBe("uploading");
        } finally {
            manager.setEnabled(false);
        }
    });
    it("reconciles a committed upload if cancellation races the checkpoint phase", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    { detail: "Already catalogued", code: "BACKUP_PENDING" },
                    { status: 409 },
                ),
            ),
        );
        const manager = managerForTest();
        manager.addJob("raced-job", "safe.txt", "/");
        await manager.cancel(manager.snapshot()[0].key);
        expect(manager.snapshot()[0].stage).toBe("backup_pending");
        expect(manager.snapshot()[0].state).toBe("uploading");
        expect(manager.snapshot()[0].error).toBeUndefined();
    });
    it("labels a failed checkpoint as a backup retry, not an upload retry", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "retry-job",
                            status: "error",
                            phase: "backup_pending",
                            name: "safe.txt",
                            current: 4,
                            total: 4,
                        },
                    ],
                }),
            ),
        );
        const manager = managerForTest();
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.snapshot()[0]?.state).toBe("error"),
            );
            expect(manager.snapshot()[0].phase).toContain(
                "Backup Needs Attention",
            );
        } finally {
            manager.setEnabled(false);
        }
    });
});

describe("overall backup readiness", () => {
    const row = (state: Transfer["state"], kind = "upload"): Transfer => ({
        key: state,
        name: "file",
        size: 4,
        lastModified: 1,
        destination: "/",
        phase: state,
        state,
        current: 0,
        total: 4,
        kind,
    });
    it("does not claim full protection while selected files are still queued or receiving", () => {
        expect(uploadBackupState([row("completed"), row("queued")])).toBe(
            "pending",
        );
        expect(uploadBackupState([row("receiving")])).toBe("pending");
        expect(uploadBackupState([row("uploading")])).toBe("pending");
    });
    it("requires failed uploads and missing originals to be resolved", () => {
        expect(uploadBackupState([row("error")])).toBe("attention");
        expect(uploadBackupState([row("awaiting_file")])).toBe("attention");
    });
    it("allows protection after completion or intentional cancellation", () => {
        expect(
            uploadBackupState([row("completed"), row("cancelled")]),
        ).toBeNull();
    });
    it("does not confuse pending deletion with an unsent upload", () => {
        expect(uploadBackupState([row("error", "delete")])).toBeNull();
    });
});

describe("deletion operation tracking", () => {
    it("keeps active and failed deletions out of upload history, including restored browser history", async () => {
        localStorage.setItem(
            "tgdrive.uploads.v2",
            JSON.stringify([
                {
                    key: "old-delete",
                    initiatedHere: true,
                    name: "Old",
                    destination: "/",
                    size: 0,
                    kind: "delete",
                    jobId: "delete-old",
                    state: "uploading",
                },
            ]),
        );
        const jobs = [
            {
                id: "delete-1",
                kind: "delete",
                status: "running",
                phase: "deleting",
                current: 0,
                total: 10,
                name: "First",
            },
            {
                id: "delete-2",
                kind: "delete",
                status: "error",
                phase: "deleting",
                current: 0,
                total: 10,
                name: "Second",
                error: "Try Again",
            },
            {
                id: "upload-1",
                kind: "upload",
                status: "running",
                phase: "uploading",
                current: 3,
                total: 10,
                name: "Upload",
            },
        ];
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ jobs })),
        );
        const manager = managerForTest();
        expect(manager.snapshot()).toEqual([]);
        try {
            manager.setEnabled(true);
            await vi.waitFor(() =>
                expect(manager.deletionSnapshot().jobs).toHaveLength(2),
            );
            expect(manager.snapshot().map((item) => item.jobId)).toEqual([
                "upload-1",
            ]);
            expect(JSON.stringify(managerForTest().snapshot())).not.toContain(
                "delete-",
            );
        } finally {
            manager.setEnabled(false);
        }
    });
    it.each(["completed", "error", "failed", "interrupted", "cancelled"])(
        "refreshes files when a tracked deletion becomes %s",
        async (terminalStatus) => {
            let status = "running";
            vi.stubGlobal(
                "fetch",
                vi.fn(async () =>
                    Response.json({
                        jobs: [
                            {
                                id: "delete-1",
                                kind: "delete",
                                status,
                                phase: "deleting",
                                current: 0,
                                total: 10,
                                name: "First",
                            },
                        ],
                    }),
                ),
            );
            const changed = vi.fn();
            window.addEventListener("tgdrive:files-changed", changed);
            const manager = managerForTest();
            try {
                manager.setEnabled(true);
                await vi.waitFor(() =>
                    expect(manager.deletionSnapshot().jobs).toHaveLength(1),
                );
                expect(changed).not.toHaveBeenCalled();
                status = terminalStatus;
                manager.refreshJobs();
                await vi.waitFor(() => expect(changed).toHaveBeenCalledOnce());
                expect(manager.snapshot()).toEqual([]);
            } finally {
                manager.setEnabled(false);
                window.removeEventListener("tgdrive:files-changed", changed);
            }
        },
    );
});

describe("connection restart admission", () => {
    it("keeps job polling and cancellation available while pausing new browser receipts", async () => {
        let polls = 0;
        let receipts = 0;
        let complete = false;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (input: string, options: RequestInit = {}) => {
                if (input === "/api/v1/jobs") {
                    polls++;
                    return Response.json({ jobs: [] });
                }
                if (input === "/api/v1/uploads" && options.method === "POST") {
                    receipts++;
                    return Response.json({
                        id: "receipt",
                        offset: 0,
                        total_size: 4,
                        chunk_size: 4,
                    });
                }
                if (options.method === "HEAD")
                    return new Response(null, {
                        headers: { "Upload-Offset": "0", "Upload-Length": "4" },
                    });
                if (options.method === "PATCH")
                    return new Response(null, {
                        status: 204,
                        headers: { "Upload-Offset": "4" },
                    });
                if (input.endsWith("/complete")) {
                    complete = true;
                    return Response.json({ id: "job" });
                }
                return Response.json({
                    id: "receipt",
                    offset: 0,
                    total_size: 4,
                    chunk_size: 4,
                    checksums: [],
                });
            }),
        );
        const manager = managerForTest();
        try {
            manager.setAdmissionPaused(true);
            manager.setEnabled(true);
            manager.add([new File(["test"], "waiting.txt")], "/", 10);
            await vi.waitFor(() => expect(polls).toBeGreaterThan(0));
            expect(receipts).toBe(0);
            manager.refreshJobs();
            await vi.waitFor(() => expect(polls).toBeGreaterThan(1));
            await manager.cancel(manager.snapshot()[0].key);
            expect(manager.snapshot()[0].state).toBe("cancelled");
            manager.add([new File(["test"], "next.txt")], "/", 10);
            expect(receipts).toBe(0);
            manager.setAdmissionPaused(false);
            await vi.waitFor(() => expect(complete).toBe(true));
            expect(receipts).toBe(1);
        } finally {
            manager.setEnabled(false);
        }
    });
});

it("cancels only uploads included in the confirmed selection", async () => {
    const manager = managerForTest();
    manager.add(
        [new File(["a"], "first.txt"), new File(["b"], "second.txt")],
        "/",
        100,
    );
    const confirmedKeys = manager.snapshot().map((item) => item.key);
    manager.add([new File(["new"], "added-later.txt")], "/", 100);
    // A file can finish while the confirmation is open; it must be kept.
    manager.snapshot()[0].state = "completed";
    await manager.cancelAll(confirmedKeys);
    expect(
        manager.snapshot().find((item) => item.name === "first.txt")?.state,
    ).toBe("completed");
    expect(
        manager.snapshot().find((item) => item.name === "second.txt")?.state,
    ).toBe("cancelled");
    expect(
        manager.snapshot().find((item) => item.name === "added-later.txt")
            ?.state,
    ).toBe("queued");
});

it("queues device files for automatic retry when staging is full", async () => {
    let receipts = 0;
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: string) => {
            if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
            receipts++;
            return Response.json(
                { detail: "Temporary Storage budget is full" },
                { status: 507 },
            );
        }),
    );
    const manager = managerForTest();
    try {
        manager.add(
            [new File(["data"], "one"), new File(["more"], "two")],
            "/",
            10,
        );
        manager.setEnabled(true);
        await vi.waitFor(() => expect(receipts).toBe(2));
        await vi.waitFor(() =>
            expect(manager.snapshot()[0].state).toBe("queued"),
        );
        expect(manager.snapshot()[0].phase).toBe("Waiting for server space");
        expect(manager.snapshot()[0].file).toBeDefined();
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(receipts).toBe(2);
    } finally {
        manager.setEnabled(false);
    }
});

it("distinguishes bytes sent from multipart messages stored in the channel", () => {
    const job = {
        id: "parts",
        status: "running",
        phase: "uploading",
        current: 3200,
        total: 4500,
        telegram_parts: { stored: 0, total: 3 },
    };
    expect(fromJob(job).phase).toBe(
        "Uploading to Telegram · 0/3 Parts in Channel",
    );
    expect(
        fromJob({ ...job, telegram_parts: { stored: 1, total: 3 } }).phase,
    ).toContain("1/3");
    expect(fromJob({ ...job, status: "completed" }).phase).toBe(
        "Available in your drive",
    );
    expect(fromJob({ ...job, telegram_parts: undefined }).phase).toBe(
        "Uploading to Telegram",
    );
});

it("keeps near-complete byte counters precise without implying completion", () => {
    const gb = 1024 ** 3;
    expect(formatByteProgress(4.2 * gb, 4.25 * gb)).toBe("4.2 / 4.25 GB");
    expect(formatByteProgress(4.2499 * gb, 4.25 * gb)).toBe("4.25 / 4.25 GB");
    expect(remainingBytesHint(4.25 * gb - 1024, 4.25 * gb)).toBe(
        "1 KB remaining",
    );
    expect(remainingBytesHint(4.25 * gb, 4.25 * gb)).toBe("");
    expect(remainingBytesHint(gb, 4.25 * gb)).toBe("");
});

describe("recovery records across reloads and open tabs", () => {
    it("restores valid receipts beside a malformed record and keeps a recovery warning", () => {
        const first = managerForTest();
        first.add([new File(["x"], "valid.txt")], "/", 10);
        localStorage.setItem("tgdrive.uploads.v2:broken", "{invalid");
        const restored = managerForTest();
        expect(restored.snapshot().map((row) => row.name)).toEqual([
            "valid.txt",
        ]);
        expect(restored.recoveryWarning).toContain(
            "Some transfer recovery information",
        );
        restored.add([new File(["x"], "new.txt")], "/", 10);
        expect(restored.recoveryWarning).toContain("Valid saved transfers");
        expect(
            managerForTest()
                .snapshot()
                .map((row) => row.name),
        ).toEqual(["valid.txt", "new.txt"]);
    });

    it("retains distinct dismissals even when two tabs write at the same time", async () => {
        const first = managerForTest();
        const second = managerForTest();
        first.addJob("first-finished", "first.txt", "/");
        second.addJob("second-finished", "second.txt", "/");
        first.snapshot()[0].state = "completed";
        second.snapshot()[0].state = "completed";
        const write = localStorage.setItem;
        let interleaved = false;
        const spy = vi
            .spyOn(localStorage, "setItem")
            .mockImplementation((key, value) => {
                if (
                    !interleaved &&
                    key === "tgdrive.dismissed-jobs.v1:first-finished"
                ) {
                    interleaved = true;
                    second.clearFinished();
                }
                write(key, value);
            });
        first.clearFinished();
        spy.mockRestore();
        expect(interleaved).toBe(true);
        expect(
            localStorage.getItem("tgdrive.dismissed-jobs.v1:first-finished"),
        ).not.toBeNull();
        expect(
            localStorage.getItem("tgdrive.dismissed-jobs.v1:second-finished"),
        ).not.toBeNull();
        localStorage.setItem("tgdrive.dismissed-jobs.v1", "{invalid");
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    jobs: [
                        {
                            id: "first-finished",
                            status: "completed",
                            phase: "Complete",
                            current: 1,
                            total: 1,
                        },
                        {
                            id: "second-finished",
                            status: "completed",
                            phase: "Complete",
                            current: 1,
                            total: 1,
                        },
                    ],
                }),
            ),
        );
        const restored = managerForTest();
        try {
            restored.setEnabled(true);
            await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
            expect(restored.snapshot()).toEqual([]);
        } finally {
            restored.setEnabled(false);
        }
    });

    it("keeps another tab's acknowledged receipt through idle and stale snapshot writes", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ jobs: [] })),
        );
        const first = managerForTest();
        const emptyTab = managerForTest();
        first.add([new File(["abcdefgh"], "source.txt")], "/", 10);
        const item = first.snapshot()[0];
        Object.assign(item, {
            uploadId: "receipt-id",
            current: 4,
            state: "receiving",
        });
        first.addJob("publish-first", "server-first", "/");
        const staleTab = managerForTest();
        Object.assign(item, { current: 6 });
        first.addJob("publish-second", "server-second", "/");
        try {
            emptyTab.setEnabled(true);
            staleTab.setEnabled(true);
            await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
            emptyTab.setEnabled(false);
            staleTab.setEnabled(false);
            // Other rows can be added without replacing the acknowledged receipt.
            emptyTab.add([new File(["x"], "second.txt")], "/", 10);
            staleTab.addJob("stale-publish", "server-third", "/");
            const restored = managerForTest().snapshot();
            expect(restored.find((row) => row.key === item.key)).toMatchObject({
                uploadId: "receipt-id",
                current: 6,
                state: "awaiting_file",
            });
            expect(restored.some((row) => row.name === "second.txt")).toBe(
                true,
            );
        } finally {
            emptyTab.setEnabled(false);
            staleTab.setEnabled(false);
        }
    });

    it("does not resurrect a cleared local receipt from a stale tab", () => {
        const first = managerForTest();
        first.add([new File(["x"], "cancelled.txt")], "/", 10);
        first.snapshot()[0].state = "cancelled";
        first.setAdmissionPaused(true);
        const stale = managerForTest();
        first.clearFinished();
        stale.snapshot()[0].phase = "Late cancelled response";
        stale.add([new File(["x"], "next.txt")], "/", 10);
        expect(
            managerForTest()
                .snapshot()
                .map((row) => row.name),
        ).toEqual(["next.txt"]);
    });

    it("keeps current v2 array receipts recoverable while new records are stored separately", () => {
        const row = {
            key: "current-v2",
            initiatedHere: true,
            name: "source.txt",
            size: 8,
            lastModified: 1,
            destination: "/",
            state: "receiving",
            phase: "Receiving",
            current: 4,
            total: 8,
            uploadId: "current-receipt",
        };
        localStorage.setItem("tgdrive.uploads.v2", JSON.stringify([row]));
        const restored = managerForTest();
        restored.add([new File(["x"], "new.txt")], "/", 10);
        const stale = managerForTest();
        expect(
            managerForTest()
                .snapshot()
                .find((item) => item.key === row.key),
        ).toMatchObject({
            uploadId: row.uploadId,
            current: 4,
            state: "awaiting_file",
        });
        restored.snapshot().find((item) => item.key === row.key)!.state =
            "cancelled";
        restored.clearFinished();
        expect(
            managerForTest()
                .snapshot()
                .map((item) => item.name),
        ).toEqual(["new.txt"]);
        expect(localStorage.getItem("tgdrive.uploads.v2")).toBeNull();
        stale.snapshot().find((item) => item.key === row.key)!.phase =
            "Late response";
        stale.addJob("unrelated", "another.txt", "/");
        expect(
            managerForTest()
                .snapshot()
                .some((item) => item.key === row.key),
        ).toBe(false);
    });

    it("bounds persisted dismissal records without rewriting older IDs", () => {
        for (let index = 0; index <= 1000; index++)
            localStorage.setItem(
                "tgdrive.dismissed-jobs.v1:" + index,
                String(index + 1),
            );
        managerForTest().add([new File(["x"], "new.txt")], "/", 10);
        const keys = Array.from({ length: localStorage.length }, (_, index) =>
            localStorage.key(index)!,
        );
        expect(
            keys.filter((key) => key.startsWith("tgdrive.dismissed-jobs.v1:")),
        ).toHaveLength(1000);
        expect(localStorage.getItem("tgdrive.dismissed-jobs.v1:0")).toBeNull();
    });

    it("does not let a stale controller overwrite another tab's dismissals", () => {
        const first = managerForTest();
        first.addJob("completed-job", "finished.jpg", "/");
        first.snapshot()[0].state = "completed";
        first.addJob("active-job", "active.mov", "/");
        const second = managerForTest();
        expect(
            second.snapshot().some((item) => item.jobId === "completed-job"),
        ).toBe(true);
        first.clearFinished();
        expect(first.snapshot().map((item) => item.jobId)).toEqual([
            "active-job",
        ]);
        // The stale tab writes after the clear, as a normal progress poll would.
        second.addJob("another-job", "another.mov", "/");
        expect(
            second.snapshot().some((item) => item.jobId === "completed-job"),
        ).toBe(false);
        expect(
            localStorage.getItem("tgdrive.dismissed-jobs.v1:completed-job"),
        ).not.toBeNull();
        expect(
            managerForTest()
                .snapshot()
                .some((item) => item.jobId === "completed-job"),
        ).toBe(false);
    });
    it("suppresses stale saved completed rows during construction", () => {
        localStorage.setItem(
            "tgdrive.dismissed-jobs.v1",
            JSON.stringify(["finished-job"]),
        );
        localStorage.setItem(
            "tgdrive.uploads.v2",
            JSON.stringify([
                {
                    key: "saved",
                    name: "finished.jpg",
                    destination: "/",
                    size: 1,
                    state: "completed",
                    current: 1,
                    total: 1,
                    lastModified: 0,
                    jobId: "finished-job",
                    initiatedHere: true,
                },
            ]),
        );
        expect(managerForTest().snapshot()).toEqual([]);
    });
});
