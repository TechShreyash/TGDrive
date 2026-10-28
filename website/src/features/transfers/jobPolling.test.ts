import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api";
import type { Job } from "../../shared/types";
import { TransferManager } from "./transfers";

const managers: TransferManager[] = [];
const requestJobs = vi.fn(async (_signal?: AbortSignal) => ({ jobs: [] as Job[] }));
let visibility: EventTarget & { visibilityState: string };
const flush = () => vi.advanceTimersByTimeAsync(0);
const managerForTest = () => {
    const manager = new TransferManager();
    managers.push(manager);
    return manager;
};

beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-28T00:00:00Z"));
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
    visibility = Object.assign(new EventTarget(), {
        visibilityState: "visible",
    });
    vi.stubGlobal("document", visibility);
    requestJobs.mockReset();
    requestJobs.mockResolvedValue({ jobs: [] });
    vi.spyOn(api, "jobs").mockImplementation(requestJobs);
});

afterEach(() => {
    for (const manager of managers.splice(0)) manager.setEnabled(false);
    vi.restoreAllMocks();
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("adaptive jobs polling", () => {
    it("polls idle visible tabs every 30 seconds and hidden tabs every 60 seconds", async () => {
        managerForTest().setEnabled(true);
        await flush();
        expect(requestJobs).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(29999);
        expect(requestJobs).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        visibility.visibilityState = "hidden";
        visibility.dispatchEvent(new Event("visibilitychange"));
        await vi.advanceTimersByTimeAsync(59999);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(3);
    });

    it("refreshes on return while coalescing visibility and focus notifications", async () => {
        visibility.visibilityState = "hidden";
        managerForTest().setEnabled(true);
        await flush();
        await vi.advanceTimersByTimeAsync(5000);
        visibility.visibilityState = "visible";
        visibility.dispatchEvent(new Event("visibilitychange"));
        window.dispatchEvent(new Event("focus"));
        await flush();
        window.dispatchEvent(new Event("focus"));
        await flush();
        expect(requestJobs).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(1500);
        window.dispatchEvent(new Event("focus"));
        await flush();
        expect(requestJobs).toHaveBeenCalledTimes(3);
    });

    it("restores the visible idle interval when a recent poll makes another request unnecessary", async () => {
        visibility.visibilityState = "hidden";
        managerForTest().setEnabled(true);
        await flush();
        await vi.advanceTimersByTimeAsync(1000);
        visibility.visibilityState = "visible";
        visibility.dispatchEvent(new Event("visibilitychange"));
        await vi.advanceTimersByTimeAsync(29999);
        expect(requestJobs).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(2);
    });

    it("never overlaps a pending poll and removes timers and listeners when disabled", async () => {
        let resolve!: (value: { jobs: Job[] }) => void;
        requestJobs.mockReturnValueOnce(
            new Promise((done) => {
                resolve = done;
            }),
        );
        const manager = managerForTest();
        manager.setEnabled(true);
        await vi.advanceTimersByTimeAsync(10000);
        window.dispatchEvent(new Event("focus"));
        visibility.dispatchEvent(new Event("visibilitychange"));
        expect(requestJobs).toHaveBeenCalledTimes(1);
        resolve({ jobs: [] });
        await flush();
        await vi.advanceTimersByTimeAsync(29999);
        expect(requestJobs).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        manager.setEnabled(false);
        window.dispatchEvent(new Event("focus"));
        visibility.dispatchEvent(new Event("visibilitychange"));
        await vi.advanceTimersByTimeAsync(60000);
        expect(requestJobs).toHaveBeenCalledTimes(2);
    });

    it("aborts a disabled poll and ignores a late response without publishing", async () => {
        let resolve!: (value: { jobs: Job[] }) => void;
        requestJobs.mockReturnValueOnce(
            new Promise((done) => {
                resolve = done;
            }),
        );
        const manager = managerForTest();
        const changed = vi.fn();
        manager.subscribe(changed);
        manager.setEnabled(true);
        const signal = requestJobs.mock.calls[0][0]!;
        manager.setEnabled(false);
        expect(signal.aborted).toBe(true);
        resolve({
            jobs: [
                {
                    id: "abandoned",
                    name: "Old session",
                    status: "completed",
                    phase: "completed",
                    current: 4,
                    total: 4,
                },
            ],
        });
        await flush();
        expect(manager.snapshot()).toEqual([]);
        expect(changed).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(60000);
        expect(requestJobs).toHaveBeenCalledTimes(1);
    });

    it("starts a fresh poll on re-enable and isolates its state from the old response", async () => {
        const releases: ((value: { jobs: Job[] }) => void)[] = [];
        requestJobs
            .mockImplementationOnce(
                () => new Promise((resolve) => releases.push(resolve)),
            )
            .mockImplementationOnce(
                () => new Promise((resolve) => releases.push(resolve)),
            );
        const manager = managerForTest();
        manager.setEnabled(true);
        manager.setEnabled(false);
        manager.setEnabled(true);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        expect(requestJobs.mock.calls[0][0]!.aborted).toBe(true);
        expect(requestJobs.mock.calls[1][0]!.aborted).toBe(false);
        const current: Job = {
            id: "current",
            name: "Current session",
            status: "running",
            phase: "uploading",
            current: 0,
            total: 4,
        };
        releases[0]({ jobs: [{ ...current, id: "old", name: "Old session" }] });
        await flush();
        expect(manager.snapshot()).toEqual([]);
        await vi.advanceTimersByTimeAsync(1500);
        window.dispatchEvent(new Event("focus"));
        expect(requestJobs).toHaveBeenCalledTimes(2);
        releases[1]({ jobs: [current] });
        await flush();
        expect(manager.snapshot().map((item) => item.jobId)).toEqual(["current"]);
        await vi.advanceTimersByTimeAsync(1499);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(3);
    });

    it.each(["upload", "delete"])(
        "keeps active %s work at 1.5 seconds even hidden, then slows after completion",
        async (kind) => {
            const job: Job = {
                id: "active",
                name: "Work",
                kind,
                status: "running",
                phase: "uploading",
                current: 0,
                total: 4,
            };
            visibility.visibilityState = "hidden";
            requestJobs.mockResolvedValue({ jobs: [job] });
            const manager = managerForTest();
            manager.setEnabled(true);
            await flush();
            expect(manager.hasActiveWork()).toBe(true);
            await vi.advanceTimersByTimeAsync(1499);
            expect(requestJobs).toHaveBeenCalledTimes(1);
            await vi.advanceTimersByTimeAsync(1);
            expect(requestJobs).toHaveBeenCalledTimes(2);
            requestJobs.mockResolvedValue({
                jobs: [{ ...job, status: "completed", phase: "completed" }],
            });
            await vi.advanceTimersByTimeAsync(1500);
            expect(manager.hasActiveWork()).toBe(false);
            expect(requestJobs).toHaveBeenCalledTimes(3);
            await vi.advanceTimersByTimeAsync(59999);
            expect(requestJobs).toHaveBeenCalledTimes(3);
            await vi.advanceTimersByTimeAsync(1);
            expect(requestJobs).toHaveBeenCalledTimes(4);
        },
    );

    it("does not postpone an active poll when focus and visibility change shortly before it", async () => {
        requestJobs.mockResolvedValue({
            jobs: [
                {
                    id: "active",
                    status: "running",
                    phase: "uploading",
                    current: 0,
                    total: 4,
                },
            ],
        });
        managerForTest().setEnabled(true);
        await flush();
        await vi.advanceTimersByTimeAsync(1000);
        visibility.visibilityState = "hidden";
        visibility.dispatchEvent(new Event("visibilitychange"));
        visibility.visibilityState = "visible";
        visibility.dispatchEvent(new Event("visibilitychange"));
        window.dispatchEvent(new Event("focus"));
        await vi.advanceTimersByTimeAsync(499);
        expect(requestJobs).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(requestJobs).toHaveBeenCalledTimes(2);
    });

    it.each([false, true])(
        "accelerates newly available device files after an idle poll, resumed=%s",
        async (resumed) => {
            if (resumed)
                localStorage.setItem(
                    "tgdrive.uploads.v2:device",
                    JSON.stringify({
                        key: "device",
                        initiatedHere: true,
                        name: "file.bin",
                        destination: "/",
                        size: 4,
                        lastModified: 0,
                        current: 0,
                        total: 4,
                        uploadId: "receipt",
                        state: "awaiting_file",
                        phase: "Choose the original file to resume",
                    }),
                );
            const manager = managerForTest();
            manager.setEnabled(true);
            await flush();
            expect(manager.hasActiveWork()).toBe(false);
            manager.setAdmissionPaused(true);
            const file = new File(["data"], "file.bin");
            if (resumed) await manager.resume("device", file);
            else manager.add([file], "/", 10);
            expect(manager.hasActiveWork()).toBe(true);
            await vi.advanceTimersByTimeAsync(1499);
            expect(requestJobs).toHaveBeenCalledTimes(1);
            await vi.advanceTimersByTimeAsync(1);
            expect(requestJobs).toHaveBeenCalledTimes(2);
        },
    );

    it("keeps checking an import whose server ownership is still being recovered", async () => {
        localStorage.setItem(
            "tgdrive.uploads.v2:remote",
            JSON.stringify({
                key: "remote",
                initiatedHere: true,
                name: "remote.bin",
                destination: "/",
                size: 0,
                lastModified: 0,
                current: 0,
                total: 0,
                kind: "remote",
                remoteRequestId: "pending-request",
                state: "uploading",
                phase: "Checking import status",
            }),
        );
        const recovery = vi
            .spyOn(api, "remoteRequest")
            .mockImplementation(() => new Promise(() => {}));
        visibility.visibilityState = "hidden";
        const manager = managerForTest();
        manager.setEnabled(true);
        await flush();
        expect(manager.hasActiveWork()).toBe(true);
        await vi.advanceTimersByTimeAsync(1500);
        expect(requestJobs).toHaveBeenCalledTimes(2);
        expect(recovery).toHaveBeenCalledTimes(1);
    });
});
