import { describe, expect, it, vi } from "vitest";
import {
    createListingRefreshQueue,
    reconcileDirectory,
    reconcileDriveItems,
} from "./listingUpdates";
import type { Directory, DriveItem } from "../../shared/types";

function deferred() {
    let resolve!: () => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<void>((success, failure) => {
        resolve = success;
        reject = failure;
    });
    return { promise, resolve, reject };
}

describe("upload-driven listing refreshes", () => {
    it("acknowledges an explicit refresh only after its trailing fresh load", async () => {
        const first = deferred();
        const second = deferred();
        const load = vi
            .fn<() => Promise<void>>()
            .mockReturnValueOnce(first.promise)
            .mockReturnValueOnce(second.promise);
        const queue = createListingRefreshQueue(load);
        const initial = queue.request();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
        const acknowledge = vi.fn();
        const manual = queue.request().then(acknowledge);
        first.resolve();
        await expect(initial).resolves.toBe(true);
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));
        expect(acknowledge).not.toHaveBeenCalled();
        second.resolve();
        await manual;
        expect(acknowledge).toHaveBeenCalledWith(true);
        queue.dispose();
    });

    it("does not report refresh success after failure or navigation", async () => {
        const queue = createListingRefreshQueue(async () => false);
        await expect(queue.request()).resolves.toBe(false);
        queue.dispose();
        const waiting = deferred();
        const other = createListingRefreshQueue(() => waiting.promise);
        const current = other.request();
        const trailing = other.request();
        other.dispose();
        await expect(current).resolves.toBe(false);
        await expect(trailing).resolves.toBe(false);
        waiting.resolve();
    });

    it("lets a slow listing finish and coalesces a completion burst into one trailing request", async () => {
        const first = deferred();
        const second = deferred();
        const load = vi
            .fn<() => Promise<void>>()
            .mockReturnValueOnce(first.promise)
            .mockReturnValueOnce(second.promise)
            .mockResolvedValue(undefined);
        const queue = createListingRefreshQueue(load);
        queue.request();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
        for (let index = 0; index < 100; index++) queue.request();
        expect(load).toHaveBeenCalledTimes(1);
        first.resolve();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));
        second.resolve();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(load).toHaveBeenCalledTimes(2);
        queue.request();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(3));
        queue.dispose();
    });

    it("retries a queued invalidation after a failed request", async () => {
        const first = deferred();
        const load = vi
            .fn<() => Promise<void>>()
            .mockReturnValueOnce(first.promise)
            .mockResolvedValue(undefined);
        const queue = createListingRefreshQueue(load);
        queue.request();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
        queue.request();
        first.reject(new Error("Network interrupted"));
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));
        queue.dispose();
    });

    it("drops stale queued refreshes when navigating to another directory", async () => {
        const first = deferred();
        const load = vi.fn(() => first.promise);
        const queue = createListingRefreshQueue(load);
        queue.request();
        await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1));
        queue.request();
        queue.dispose();
        first.resolve();
        queue.request();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(load).toHaveBeenCalledTimes(1);
        const untouched = vi.fn(async () => {});
        const cancelled = createListingRefreshQueue(untouched);
        cancelled.request();
        cancelled.dispose();
        await Promise.resolve();
        expect(untouched).not.toHaveBeenCalled();
    });
});

const file: DriveItem = {
    id: "file",
    name: "File.txt",
    path: "/folder",
    type: "file",
    size: 30,
    parent_names: ["Folder"],
};
const folder: DriveItem = {
    id: "folder",
    name: "Folder",
    path: "/folder",
    type: "folder",
};

describe("incremental directory updates", () => {
    it("updates a directory's read-only state even when its rows are unchanged", () => {
        const previous: Directory = { contents: {}, read_only: false };
        const restricted = reconcileDirectory(previous, {
            contents: {},
            read_only: true,
        });
        expect(restricted.read_only).toBe(true);
        expect(restricted).not.toBe(previous);
        expect(restricted.contents).toBe(previous.contents);
        expect(
            reconcileDirectory(restricted, { contents: {}, read_only: false })
                .read_only,
        ).toBe(false);
    });
    it("reuses an unchanged listing and rows across fresh JSON responses", () => {
        const previous: Directory = {
            name: "Folder",
            contents: { file, folder },
        };
        const next = JSON.parse(JSON.stringify(previous)) as Directory;
        expect(reconcileDirectory(previous, next)).toBe(previous);
    });

    it("inserts new uploads while preserving existing rows and accepts later removals", () => {
        const previous: Directory = { contents: { file, folder } };
        const uploaded = { ...file, id: "new-file", name: "Uploaded.txt" };
        const result = reconcileDirectory(previous, {
            contents: {
                file: { ...file },
                folder: { ...folder },
                "new-file": uploaded,
            },
        });
        expect(result).not.toBe(previous);
        expect(result.contents.file).toBe(file);
        expect(result.contents.folder).toBe(folder);
        expect(result.contents["new-file"]).toBe(uploaded);
        const removed = reconcileDirectory(result, { contents: { folder } });
        expect(removed.contents).toEqual({ folder });
    });

    it("applies changed metadata and folder names without replacing untouched rows", () => {
        const previous: Directory = {
            name: "Before",
            contents: { file, folder },
        };
        const changed = { ...file, size: 50, parent_names: ["Renamed Folder"] };
        const result = reconcileDirectory(previous, {
            name: "After",
            contents: { file: changed, folder: { ...folder } },
        });
        expect(result.name).toBe("After");
        expect(result.contents.file).toBe(changed);
        expect(result.contents.folder).toBe(folder);
    });

    it("preserves unchanged search results and follows server ordering after an upload", () => {
        const previous = [file, folder];
        expect(
            reconcileDriveItems(previous, [{ ...file }, { ...folder }]),
        ).toBe(previous);
        const reordered = reconcileDriveItems(previous, [
            { ...folder },
            { ...file },
        ]);
        expect(reordered).toEqual([folder, file]);
        expect(reordered[0]).toBe(folder);
        expect(reordered[1]).toBe(file);
    });
});
