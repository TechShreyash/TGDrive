import { describe, expect, it } from "vitest";
import { historyStatus, mergeHistory, visibleHistory } from "./transferHistory";
import { canCancelTransfer, type Transfer } from "./transfers";
import type { Job } from "../../shared/types";

function local(change: Partial<Transfer> = {}): Transfer {
    return {
        key: "local",
        initiatedHere: true,
        name: "file.bin",
        size: 100,
        total: 100,
        current: 0,
        lastModified: 0,
        destination: "/",
        state: "queued",
        phase: "Waiting",
        folderUpload: change.relativePath
            ? {
                  id: change.batchId || "folder",
                  name: "Folder",
                  root_path: "/folder",
                  created_at: 1,
              }
            : undefined,
        ...change,
    };
}
function job(change: Partial<Job> = {}): Job {
    return {
        id: "server",
        name: "old.bin",
        kind: "upload",
        status: "completed",
        phase: "completed",
        total: 100,
        current: 100,
        created_at: 1000,
        finished_at: 1100,
        ...change,
    };
}
describe("history merged with live browser work", () => {
    it("keeps live running and queued batches above every finished filter", () => {
        const active = local({ key: "active", state: "receiving" });
        const queued = local({ key: "queued", state: "queued" });
        const rows = visibleHistory(
            [job(), job({ id: "failure", status: "error" })],
            [active, queued],
            "error",
            true,
        );
        expect(rows.map((row) => row.jobId || row.key).sort()).toEqual([
            "active",
            "failure",
            "queued",
        ]);
    });
    it("does not prepend unrelated recent completions to an older cursor page", () => {
        const rows = visibleHistory(
            [job({ id: "older" })],
            [
                local({ key: "recent", state: "completed" }),
                local({ key: "live", state: "receiving" }),
            ],
            "all",
            false,
        );
        expect(rows.map((row) => row.jobId || row.key).sort()).toEqual([
            "live",
            "older",
        ]);
    });
    it("keeps the first server page bounded when older job-backed completions remain locally", () => {
        const rows = visibleHistory(
            [job({ id: "newest" })],
            [
                local({
                    key: "old-local",
                    jobId: "archived-older",
                    state: "completed",
                }),
                local({ key: "receipt", state: "error" }),
            ],
            "all",
            true,
        );
        expect(rows.map((row) => row.jobId || row.key).sort()).toEqual([
            "newest",
            "receipt",
        ]);
    });
    it("preserves completed siblings inside a live folder on older pages", () => {
        const rows = visibleHistory(
            [job({ id: "older" })],
            [
                local({
                    key: "live",
                    batchId: "folder",
                    relativePath: "Folder/live.bin",
                    state: "receiving",
                }),
                local({
                    key: "sibling",
                    batchId: "folder",
                    relativePath: "Folder/sibling.bin",
                    state: "completed",
                }),
                local({ key: "unrelated", state: "completed" }),
            ],
            "all",
            false,
        );
        expect(rows.map((row) => row.jobId || row.key).sort()).toEqual([
            "live",
            "older",
            "sibling",
        ]);
    });
    it("keeps cleared server records and unfinished browser-only transfers visible", () => {
        const rows = mergeHistory([job()], [local()], "all");
        expect(rows).toHaveLength(2);
        const past = rows.find((item) => item.jobId === "server")!;
        expect(past.state).toBe("completed");
        expect(past.phase).toBe("Upload completed");
        expect(past.historyOnly).toBe(true);
        expect(canCancelTransfer(past)).toBe(false);
        expect(mergeHistory([job()], [], "completed")).toHaveLength(1);
    });
    it("uses current retry/progress state without duplicating a history entry", () => {
        const current = local({
            jobId: "server",
            state: "uploading",
            stage: "uploading",
            current: 40,
        });
        const rows = mergeHistory(
            [job({ status: "error" })],
            [current],
            "running",
        );
        expect(rows).toHaveLength(1);
        expect(rows[0]).toBe(current);
        expect(
            mergeHistory([job({ status: "error" })], [current], "error"),
        ).toEqual([]);
    });
    it("distinguishes browser/server queue, active work, failures and cancellations", () => {
        expect(historyStatus(local())).toBe("queued");
        expect(historyStatus(local({ state: "receiving" }))).toBe("running");
        expect(
            historyStatus(
                local({ state: "uploading", serverStatus: "queued" }),
            ),
        ).toBe("queued");
        expect(historyStatus(local({ state: "cancelling" }))).toBe("running");
        expect(historyStatus(local({ state: "awaiting_file" }))).toBe("error");
        expect(historyStatus(local({ state: "cancelled" }))).toBe("cancelled");
        expect(mergeHistory([job({ kind: "delete" })], [], "all")).toEqual([]);
    });
});

it("retains the current catalog destination when live progress replaces history", () => {
    const location = {
        available: true,
        folder_path: "/moved",
        folder_label: "My Files / Saved",
        item_id: "exact",
    };
    const rows = mergeHistory(
        [job({ location })],
        [local({ jobId: "server", state: "completed" })],
        "all",
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].location).toEqual(location);
});

it("filters whole folders instead of extracting matching children into duplicate groups", () => {
    const items = [
        local({
            key: "one",
            batchId: "folder",
            relativePath: "Folder/one.bin",
            state: "uploading",
            stage: "uploading",
        }),
        local({
            key: "two",
            batchId: "folder",
            relativePath: "Folder/two.bin",
            state: "queued",
        }),
        local({
            key: "three",
            batchId: "folder",
            relativePath: "Folder/three.bin",
            state: "completed",
        }),
    ];
    expect(mergeHistory([], items, "running")).toHaveLength(3);
    expect(mergeHistory([], items, "completed")).toEqual([]);
    expect(mergeHistory([], items, "queued")).toEqual([]);
    const waiting = items.map((item) =>
        item.key === "one" ? { ...item, state: "queued" as const } : item,
    );
    expect(mergeHistory([], waiting, "queued")).toHaveLength(3);
    expect(mergeHistory([], waiting, "completed")).toEqual([]);
    const finished = items.map((item) => ({
        ...item,
        state: "completed" as const,
    }));
    expect(mergeHistory([], finished, "completed")).toHaveLength(3);
});
