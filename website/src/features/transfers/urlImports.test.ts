import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../../shared/api";
import { fromJob, TransferManager } from "./transfers";
import type { Job } from "../../shared/types";

const job: Job = {
    id: "remote-job",
    kind: "remote",
    name: "movie.mov",
    status: "running",
    phase: "checking_source",
    current: 0,
    total: 0,
};
const requestId = "url-import-request-0001";
function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
        resolve = done;
    });
    return { promise, resolve };
}
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

it("publishes an immediate source-check row while the start request is pending", async () => {
    const response = deferred<{ status: string; id: string }>();
    vi.spyOn(api, "remote").mockReturnValue(response.promise);
    const manager = new TransferManager();
    manager.startRemote(
        "https://files.example/movie.mov?token=private",
        "/",
        false,
        requestId,
    );
    expect(manager.snapshot()).toEqual([
        expect.objectContaining({
            phase: "Starting URL import…",
            stage: "checking_source",
            remoteRequestId: requestId,
        }),
    ]);
    expect(
        localStorage.getItem("tgdrive.uploads.v2:" + manager.snapshot()[0].key),
    ).not.toContain("token=private");
    response.resolve({ status: "ok", id: job.id });
    await vi.waitFor(() => expect(manager.snapshot()[0].jobId).toBe(job.id));
});

it("cancels by request identity before the job ID arrives and ignores a late start", async () => {
    const response = deferred<{ status: string; id: string }>();
    vi.spyOn(api, "remote").mockReturnValue(response.promise);
    const cancel = vi.spyOn(api, "cancelRemoteRequest").mockResolvedValue({
        id: job.id,
        data: { ...job, status: "cancelled" },
    });
    const manager = new TransferManager();
    const key = manager.startRemote(
        "https://files.example/movie.mov",
        "/",
        false,
        requestId,
    );
    await manager.cancel(key);
    expect(cancel).toHaveBeenCalledWith(requestId);
    response.resolve({ status: "ok", id: job.id });
    await Promise.resolve();
    await Promise.resolve();
    expect(manager.snapshot()[0].state).toBe("cancelled");
});

it("recovers a lost start response without posting a second import", async () => {
    const post = vi
        .spyOn(api, "remote")
        .mockRejectedValue(new Error("response lost"));
    vi.spyOn(api, "remoteRequest").mockResolvedValue({ id: job.id, data: job });
    const manager = new TransferManager();
    manager.startRemote(
        "https://files.example/movie.mov",
        "/",
        false,
        requestId,
    );
    await vi.waitFor(() => expect(manager.snapshot()[0].jobId).toBe(job.id));
    expect(post).toHaveBeenCalledTimes(1);
    expect(manager.snapshot()[0].state).toBe("uploading");
});

it("merges a job found by polling before the start response", async () => {
    const response = deferred<{ status: string; id: string }>();
    vi.spyOn(api, "remote").mockReturnValue(response.promise);
    const manager = new TransferManager();
    manager.startRemote(
        "https://files.example/movie.mov",
        "/",
        false,
        requestId,
    );
    manager.addJob(job.id, "movie.mov", "/", "remote");
    response.resolve({ status: "ok", id: job.id });
    await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(1));
    expect(manager.snapshot()[0].remoteRequestId).toBe(requestId);
});

it("names queue and storage states without a made-up preflight percentage", () => {
    expect(fromJob(job, "remote").phase).toBe("Checking source…");
    expect(fromJob({ ...job, download_mode: "queued" }, "remote").phase).toBe(
        "Waiting for a source connection",
    );
    expect(
        fromJob(
            { ...job, phase: "downloading", download_mode: "warm" },
            "remote",
        ).phase,
    ).toBe("Queued · receiving slowly");
    expect(
        fromJob({ ...job, download_mode: "waiting_storage" }, "remote").phase,
    ).toBe("Waiting for temporary storage");
});

it("shows a source retry countdown and its bounded attempt count", () => {
    vi.spyOn(Date, "now").mockReturnValue(100_000);
    const state = fromJob(
        {
            ...job,
            phase: "source_retry_wait",
            retry_at: 104,
            source_attempt: 2,
            source_max_attempts: 4,
        },
        "remote",
    );
    expect(state.state).toBe("uploading");
    expect(state.phase).toBe(
        "Source interrupted · retrying automatically in 4s · attempt 2 of 4",
    );
    expect(state.phase).not.toContain("Telegram");
});
