import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TransferManager } from "./transfers";
import type { UploadChunkTransport } from "./uploadTransport";
import { stubDeviceLocks } from "./transferTestLocks";

const managers: TransferManager[] = [];
function manager(send?: UploadChunkTransport) {
    const result = new TransferManager(
        send || ((url, options) => fetch(url, options)),
    );
    managers.push(result);
    return result;
}
function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
        resolve = done;
    });
    return { promise, resolve };
}
const snapshot = () => ({
    id: "receipt",
    offset: 0,
    total_size: 4,
    chunk_size: 4,
    checksums: [],
});
const received = () =>
    new Response(null, { status: 204, headers: { "Upload-Offset": "4" } });
const holdChunk: UploadChunkTransport = (_url, options) =>
    new Promise((_resolve, reject) => {
        options.signal.addEventListener(
            "abort",
            () => reject(new DOMException("Paused", "AbortError")),
            { once: true },
        );
    });

beforeEach(() => {
    stubDeviceLocks();
    vi.stubGlobal("window", new EventTarget());
    const values = new Map<string, string>();
    const changed = (key: string, newValue: string | null) => {
        window.dispatchEvent(
            Object.assign(new Event("storage"), { key, newValue }),
        );
    };
    vi.stubGlobal("localStorage", {
        get length() {
            return values.size;
        },
        key: (index: number) => [...values.keys()][index] ?? null,
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => {
            values.set(key, value);
            changed(key, value);
        },
        removeItem: (key: string) => {
            values.delete(key);
            changed(key, null);
        },
    });
});
afterEach(() => {
    for (const item of managers.splice(0)) item.setEnabled(false);
    vi.unstubAllGlobals();
});

function backend(created: ReturnType<typeof deferred<Response>>) {
    const calls = vi.fn(async (url: string, options: RequestInit = {}) => {
        if (url === "/api/v1/jobs") return Response.json({ jobs: [] });
        if (url === "/api/v1/uploads") return created.promise;
        if (url.endsWith("/complete")) return Response.json({ id: "receipt" });
        if (url.endsWith("/cancel"))
            return Response.json({ status: "cancelled" });
        if (options.method === "HEAD")
            return new Response(null, {
                headers: { "Upload-Offset": "0", "Upload-Length": "4" },
            });
        return Response.json(snapshot());
    });
    vi.stubGlobal("fetch", calls);
    return calls;
}

describe("device receipt ownership across tabs", () => {
    it("refreshes a stale resume after the owner stops without creating a second receipt", async () => {
        const created = deferred<Response>();
        const requests = backend(created);
        const sending = vi.fn(holdChunk);
        const owner = manager(sending);
        const file = new File(["data"], "source.bin");
        owner.add([file], "/", 10);
        const key = owner.snapshot()[0].key;
        const stale = manager(async () => received());
        expect(stale.snapshot()[0].uploadId).toBeUndefined();
        owner.setEnabled(true);
        stale.setEnabled(true);
        await vi.waitFor(() =>
            expect(fetch).toHaveBeenCalledWith(
                "/api/v1/uploads",
                expect.anything(),
            ),
        );
        created.resolve(Response.json(snapshot()));
        await vi.waitFor(() => expect(sending).toHaveBeenCalledOnce());
        await stale.resume(key, file);
        expect(owner.snapshot()[0]).toMatchObject({
            state: "receiving",
            uploadId: "receipt",
            file,
        });
        expect(owner.snapshot()[0].controller?.signal.aborted).toBe(false);
        expect(stale.snapshot()[0].file).toBe(file);
        owner.setEnabled(false);
        await vi.waitFor(() =>
            expect(stale.snapshot()[0].jobId).toBe("receipt"),
        );
        expect(
            requests.mock.calls.filter(([url]) => url === "/api/v1/uploads"),
        ).toHaveLength(1);
    });

    it("cancels the latest receipt from a stale tab and stops the active owner", async () => {
        const created = deferred<Response>();
        backend(created);
        const sending = vi.fn(holdChunk);
        const owner = manager(sending);
        owner.add([new File(["data"], "source.bin")], "/", 10);
        const key = owner.snapshot()[0].key;
        const stale = manager();
        owner.setEnabled(true);
        stale.setEnabled(true);
        created.resolve(Response.json(snapshot()));
        await vi.waitFor(() => expect(sending).toHaveBeenCalledOnce());
        await stale.cancel(key);
        expect(fetch).toHaveBeenCalledWith(
            "/api/v1/uploads/receipt/cancel",
            expect.anything(),
        );
        expect(stale.snapshot()[0]).toMatchObject({
            state: "cancelled",
            uploadId: "receipt",
        });
        expect(owner.snapshot()[0]).toMatchObject({
            state: "cancelled",
            uploadId: "receipt",
            file: undefined,
        });
    });

    it("rejects unsupported coordination before creating a device receipt", async () => {
        vi.stubGlobal("navigator", {});
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ jobs: [] })),
        );
        const current = manager();
        current.add([new File(["data"], "source.bin")], "/", 10);
        current.setEnabled(true);
        await vi.waitFor(() =>
            expect(current.snapshot()[0].state).toBe("error"),
        );
        expect(current.snapshot()[0].error).toContain("Web Locks");
        expect(current.snapshot()[0].file).toBeDefined();
        expect(fetch).not.toHaveBeenCalledWith(
            "/api/v1/uploads",
            expect.anything(),
        );
    });

    it("does not discard an in-memory source when its saved record becomes unreadable", async () => {
        const current = manager();
        const file = new File(["data"], "source.bin");
        current.add([file], "/", 10);
        const item = current.snapshot()[0];
        Object.assign(item, { state: "error", uploadId: "receipt" });
        current.clearFinished();
        localStorage.setItem("tgdrive.uploads.v2:" + item.key, "{broken");
        await expect(current.resume(item.key, file)).rejects.toThrow(
            "unreadable",
        );
        expect(current.snapshot()[0]).toMatchObject({
            uploadId: "receipt",
            file,
        });
    });
});
