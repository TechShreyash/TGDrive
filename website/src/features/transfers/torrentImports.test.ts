import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../../shared/api";
import type { Job } from "../../shared/types";
import { deviceUploadMessage, needsDeviceUpload } from "./deviceUploadStatus";
import { transferDetails, transferProgress } from "./progress";
import { fromJob, TransferManager, type Transfer } from "./transfers";

const magnet = "magnet:?xt=urn:btih:" + "a".repeat(40) + "&dn=Photos";
const requestId = "torrent-import-request-0001";
const job: Job = {
    id: "torrent-job",
    kind: "torrent",
    name: "Photos",
    status: "running",
    phase: "torrent_metadata",
    current: 0,
    total: 0,
};

beforeEach(() => {
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

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

it("starts a server import and keeps private source parameters out of recovery storage", async () => {
    const post = vi.spyOn(api, "torrent").mockResolvedValue({ id: job.id });
    const url = "https://files.example/archive.torrent?token=private";
    const manager = new TransferManager();
    manager.startTorrent(url, "/photos", requestId);
    expect(post).toHaveBeenCalledWith(url, "/photos", requestId);
    await vi.waitFor(() => expect(manager.snapshot()[0].jobId).toBe(job.id));
    const row = manager.snapshot()[0];
    expect(row.kind).toBe("torrent");
    expect(row.phase).toBe("Reading torrent metadata…");
    expect(needsDeviceUpload(row)).toBe(false);
    expect(deviceUploadMessage([row])).toBeNull();
    expect(localStorage.getItem("tgdrive.uploads.v2:" + row.key)).not.toContain(
        "token=private",
    );
});

it("recovers a lost start response without creating another torrent import", async () => {
    const post = vi.spyOn(api, "torrent").mockRejectedValue(new Error("lost"));
    vi.spyOn(api, "remoteRequest").mockResolvedValue({ id: job.id, data: job });
    const manager = new TransferManager();
    manager.startTorrent(magnet, "/", requestId);
    await vi.waitFor(() => expect(manager.snapshot()[0].jobId).toBe(job.id));
    expect(post).toHaveBeenCalledOnce();
    expect(manager.snapshot()[0].kind).toBe("torrent");
});

it("retains torrent origin after reload and cancels an unconfirmed start by request identity", async () => {
    let resolve!: (value: { id: string }) => void;
    vi.spyOn(api, "torrent").mockReturnValue(
        new Promise((done) => {
            resolve = done;
        }),
    );
    const manager = new TransferManager();
    const key = manager.startTorrent(magnet, "/", requestId);
    const restored = new TransferManager();
    expect(restored.snapshot()[0]).toMatchObject({
        key,
        kind: "torrent",
        state: "uploading",
        phase: "Checking import status…",
        remoteRequestId: requestId,
    });
    const cancel = vi.spyOn(api, "cancelRemoteRequest").mockResolvedValue({
        id: job.id,
        data: { ...job, status: "cancelled" },
    });
    await restored.cancel(key);
    expect(cancel).toHaveBeenCalledWith(requestId);
    expect(restored.snapshot()[0].state).toBe("cancelled");
    resolve({ id: job.id });
    await Promise.resolve();
});

it("rejects invalid magnets, credentials and unsupported protocols before starting", () => {
    const post = vi.spyOn(api, "torrent");
    const manager = new TransferManager();
    for (const url of [
        "not a link",
        "magnet:?xt=urn:btmh:unsupported",
        "magnet:?xt=urn:btih:invalid",
        "file:///archive.torrent",
        "https://user:secret@files.example/archive.torrent",
    ])
        expect(() => manager.startTorrent(url, "/", requestId)).toThrow();
    expect(post).not.toHaveBeenCalled();
    expect(manager.snapshot()).toHaveLength(0);
});

it("keeps metadata indeterminate and reports the actual download and upload stages", () => {
    const metadata = fromJob(job, "torrent");
    expect(metadata).toMatchObject({
        kind: "torrent",
        phase: "Reading torrent metadata…",
        stage: "torrent_metadata",
    });
    expect(transferProgress(metadata as Transfer).fraction).toBeUndefined();
    expect(fromJob({ ...job, phase: "downloading" }).phase).toBe(
        "Downloading from torrent peers",
    );
    const uploading = fromJob({
        ...job,
        phase: "torrent_uploading",
        current: 50,
        total: 100,
        destination: "/photos/torrent-root",
        torrent_files: { total: 3, completed: 1 },
        speed_bytes_per_second: 10,
        progress_updated_at: 2,
    });
    expect(uploading.phase).toBe("Importing files to Telegram · 1/3 files");
    expect(uploading).toMatchObject({
        name: "Photos",
        batchName: "Photos",
        size: 100,
        destination: "/photos/torrent-root",
    });
    expect(transferDetails(uploading as Transfer, 2100).rate).toBe(10);
    expect(fromJob({ ...job, kind: "upload" }, "torrent").kind).toBe("torrent");
});
