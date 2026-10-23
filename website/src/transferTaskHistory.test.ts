import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import {
  liveHistoryRows,
  loadTaskHistoryPage,
  matchHistoryTasks,
  visibleHistory,
} from "./transferHistory";
import { summarizeBatches } from "./transferBatches";
import type { Transfer } from "./transfers";
import type { Job } from "./types";

afterEach(() => vi.restoreAllMocks());

const local = (change: Partial<Transfer> = {}): Transfer => ({
  key: "local",
  name: "file.bin",
  size: 4,
  lastModified: 0,
  destination: "/root",
  state: "error",
  phase: "Upload interrupted",
  current: 0,
  total: 4,
  createdAt: 1000,
  ...change,
});
const job = (index: number): Job => ({
  id: "job-" + index,
  name: "file-" + index,
  status: "completed",
  phase: "completed",
  current: 4,
  total: 4,
  created_at: 100 - index,
});
const folder = (count = 72) => Array.from({ length: count }, (_, index) => local({
  key: "folder-" + index,
  name: "file-" + index,
  relativePath: "Folder/file-" + index,
  batchId: "folder-selection-id",
  batchName: "Folder",
  // One failed device file keeps this whole task browser-owned.
  ...(index ? { jobId: "member-" + index, state: "completed" as const } : {}),
}));

function serverHistory(count: number) {
  return vi.spyOn(api, "transferTaskHistory").mockImplementation(
    async (_status, page, _signal, _query, context = {}) => {
      const offset = context.offset || 0;
      return {
        jobs: Array.from({ length: Math.min(20, Math.max(0, count - offset)) }, (_, index) => job(offset + index)),
        page,
        page_size: 20,
        total_tasks: count,
        total_pages: Math.max(1, Math.ceil(count / 20)),
        has_more: offset + 20 < count,
      };
    },
  );
}

describe("finished transfer task pages", () => {
  it("counts a remembered 72-file folder once and fills the other 19 task slots", async () => {
    const backend = serverHistory(25);
    const localFolder = folder();
    const page = await loadTaskHistoryPage(localFolder, "all", 1, new AbortController().signal);
    expect(page.total_tasks).toBe(26);
    expect(page.total_pages).toBe(2);
    expect(page.localItems).toHaveLength(72);
    expect(page.jobs).toHaveLength(19);
    const rows = visibleHistory(page.jobs, page.localItems, "all", true, true);
    expect(summarizeBatches(rows, 0)).toHaveLength(20);
    expect(summarizeBatches(rows, 0).find((task) => task.folder)?.count).toBe(72);
    const context = backend.mock.calls[0][4]!;
    expect(context.exclude_folders).toEqual(["folder-selection-id"]);
    expect(context.folders?.[0].transfer_ids).toHaveLength(71);
    expect(context.folders?.[0].folder_upload.root_path).toBe("/root");
  });

  it("uses the server offset after browser-only tasks without skipping history", async () => {
    const backend = serverHistory(25);
    const page = await loadTaskHistoryPage(folder(), "all", 2, new AbortController().signal);
    expect(backend.mock.calls[0][4]?.offset).toBe(19);
    expect(page.localItems).toEqual([]);
    expect(page.jobs.map((row) => row.id)).toEqual(
      Array.from({ length: 6 }, (_, index) => "job-" + (index + 19)),
    );
  });

  it("keeps ordinary selections separate when browser-only work spans pages", async () => {
    const backend = serverHistory(3);
    const files = Array.from({ length: 25 }, (_, index) => local({
      key: "file-" + index,
      batchId: "old-shared-selection",
      batchName: "25 files",
    }));
    const page = await loadTaskHistoryPage(files, "all", 2, new AbortController().signal);
    expect(page.total_tasks).toBe(28);
    expect(page.localItems).toHaveLength(5);
    expect(page.jobs).toHaveLength(3);
    expect(backend.mock.calls[0][4]?.offset).toBe(0);
    expect(summarizeBatches(page.localItems, 0)).toHaveLength(5);
  });

  it("returns a reachable page when history shrinks", async () => {
    serverHistory(1);
    const page = await loadTaskHistoryPage([], "all", 5, new AbortController().signal);
    expect(page.page).toBe(1);
    expect(page.total_pages).toBe(1);
  });

  it("keeps a locally pending folder out of finished server pages", async () => {
    const backend = serverHistory(0);
    const rows = folder(3);
    rows[0].state = "receiving";
    const page = await loadTaskHistoryPage(rows, "all", 1, new AbortController().signal);
    expect(page.total_tasks).toBe(0);
    expect(page.localItems).toEqual([]);
    expect(backend.mock.calls[0][4]?.exclude_folders).toEqual(["folder-selection-id"]);
    expect(liveHistoryRows(rows)).toHaveLength(3);
  });

  it("normalizes older folder identities without changing stored recovery data", async () => {
    const backend = serverHistory(0);
    const rows = folder(2).map((item) => ({ ...item, batchId: "old:folder" }));
    await loadTaskHistoryPage(rows, "all", 1, new AbortController().signal);
    const context = backend.mock.calls[0][4]!;
    expect(context.folders?.[0].folder_upload.id).toMatch(/^legacy-[a-f0-9]{64}$/);
    expect(context.exclude_folders).toEqual([context.folders?.[0].folder_upload.id]);
    expect(rows.every((item) => item.batchId === "old:folder")).toBe(true);
  });

  it("bounds migration hints even when old browser recovery storage is larger", async () => {
    const backend = serverHistory(0);
    const rows = Array.from({ length: 1001 }, (_, index) => local({
      key: "legacy-" + index,
      batchId: "legacy-folder-selection-" + index,
      relativePath: "Folder/file.bin",
      state: "uploading",
      jobId: "server-" + index,
    }));
    await loadTaskHistoryPage(rows, "all", 1, new AbortController().signal);
    const context = backend.mock.calls[0][4]!;
    expect(context.folders).toHaveLength(1000);
    expect(context.folders!.reduce((count, hint) => count + hint.transfer_ids.length, 0)).toBeLessThanOrEqual(2000);
    expect(new TextEncoder().encode(JSON.stringify(context)).length).toBeLessThan(256 * 1024);
  });

  it("searches a whole folder while ordinary files match independently", () => {
    const rows = folder(3);
    expect(matchHistoryTasks(rows, "file-1")).toHaveLength(3);
    expect(matchHistoryTasks([
      local({ key: "one", name: "one.txt", batchId: "old-selection" }),
      local({ key: "two", name: "two.txt", batchId: "old-selection" }),
    ], "one.txt").map((row) => row.key)).toEqual(["one"]);
  });
});
