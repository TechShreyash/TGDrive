import { describe, expect, it, vi } from "vitest";
import {
    applyPage,
    emptyFolderPages,
    folderComplete,
    loadPrefix,
    pageRequest,
    reconcilePages,
    removeRows,
    type PageRequest,
} from "./folderPages";
import type { DriveItem, NodePage } from "../../shared/types";

const item = (index: number, name = "file-" + index): DriveItem => ({
    id: "id-" + index,
    name,
    path: "/",
    type: "file",
});

/** A fake server folder of `total` items honoring limit, offset and cursor. */
function server(total: number, name?: (index: number) => string) {
    const fetch = vi.fn(async (request: PageRequest): Promise<NodePage> => {
        const start = request.cursor
            ? Number(request.cursor)
            : request.around
              ? Number(request.around.slice(3))
              : request.offset || 0;
        const end = Math.min(total, start + request.limit);
        return {
            items: Array.from({ length: end - start }, (_, index) =>
                item(start + index, name?.(start + index)),
            ),
            total,
            offset: request.offset || 0,
            next_cursor: end < total ? String(end) : null,
            ...(request.cursor ? {} : { folder: folderRecord }),
            ...(request.around ? { index: start } : {}),
        };
    });
    return fetch;
}
const folderRecord = {
    ...item(-1, "Folder"),
    type: "folder" as const,
    breadcrumbs: [],
};

describe("cursor-paged folder cache", () => {
    it("loads 200 rows first, then continues the cursor and jumps by offset", async () => {
        const fetch = server(1000);
        let state = await loadPrefix(emptyFolderPages, fetch);
        expect(fetch).toHaveBeenCalledWith({ offset: 0, limit: 200 });
        expect([state.loaded, state.total, state.cursor]).toEqual([
            200,
            1000,
            "200",
        ]);
        expect(state.folder?.name).toBe("Folder");
        expect(pageRequest(state, 1)).toBeNull();
        expect(pageRequest(state, 2)).toEqual({ cursor: "200", limit: 500 });
        expect(pageRequest(state, 7)).toEqual({ offset: 700, limit: 100 });

        let request = pageRequest(state, 7)!;
        state = applyPage(state, request, await fetch(request));
        expect(state.rows[700]?.id).toBe("id-700");
        expect(state.rows[300]).toBeUndefined();
        expect(state.loaded).toBe(200);
        expect(pageRequest(state, 7)).toBeNull();

        request = pageRequest(state, 2)!;
        state = applyPage(state, request, await fetch(request));
        expect([state.loaded, state.cursor]).toEqual([700, "700"]);
        // Rows 700–799 were loaded by offset; the prefix still continues at 700.
        expect(pageRequest(state, 7)).toBeNull();
        expect(pageRequest(state, 8)).toEqual({ offset: 800, limit: 100 });
        expect(folderComplete(state)).toBe(false);
    });

    it("places an around page at its absolute index", async () => {
        const fetch = server(1000);
        const state = await loadPrefix(emptyFolderPages, fetch);
        const request = { around: "id-456", limit: 1 };
        const next = applyPage(state, request, await fetch(request));
        expect(next.rows[456]?.id).toBe("id-456");
        expect(next.loaded).toBe(200);
    });

    it("ignores a cursor page that a refresh made stale", async () => {
        const fetch = server(1000);
        const state = await loadPrefix(emptyFolderPages, fetch);
        const page = await fetch({ cursor: "200", limit: 500 });
        expect(
            applyPage(
                { ...state, cursor: "other" },
                { cursor: "200", limit: 500 },
                page,
            ).loaded,
        ).toBe(200);
    });

    it("refreshes the loaded prefix in 500-item cursor steps and keeps unchanged rows", async () => {
        const fetch = server(1300);
        let state = await loadPrefix(emptyFolderPages, fetch);
        state = applyPage(
            state,
            { cursor: "200", limit: 500 },
            await fetch({ cursor: "200", limit: 500 }),
        );
        state = applyPage(
            state,
            { cursor: "700", limit: 500 },
            await fetch({ cursor: "700", limit: 500 }),
        );
        expect(state.loaded).toBe(1200);
        fetch.mockClear();
        const renamed = server(1300, (index) =>
            index === 5 ? "renamed" : "file-" + index,
        );
        const fresh = await loadPrefix(state, renamed);
        expect(renamed.mock.calls.map(([request]) => request)).toEqual([
            { offset: 0, limit: 500 },
            { cursor: "500", limit: 500 },
            { cursor: "1000", limit: 200 },
        ]);
        const merged = reconcilePages(state, fresh);
        expect(merged.loaded).toBe(1200);
        expect(merged.rows[4]).toBe(state.rows[4]);
        expect(merged.rows[5]).not.toBe(state.rows[5]);
        expect(merged.rows[5]?.name).toBe("renamed");
        expect(merged.folder).toBe(state.folder);
        expect(reconcilePages(merged, await loadPrefix(merged, renamed))).toBe(
            merged,
        );
    });

    it("marks a small folder complete and drops deleted rows", async () => {
        const fetch = server(3);
        const state = await loadPrefix(emptyFolderPages, fetch);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(folderComplete(state)).toBe(true);
        const next = removeRows(state, (row) => row.id === "id-1");
        expect(next.rows.map((row) => row?.id)).toEqual(["id-0", "id-2"]);
        expect([next.total, next.loaded]).toEqual([2, 2]);
        expect(removeRows(next, () => false)).toBe(next);
    });
});

describe("cursor-only listings (Recent)", () => {
    it("keeps Next Page reachable through the cursor without a server total", async () => {
        const rows = Array.from({ length: 180 }, (_, index) => item(index));
        const fetch = vi.fn(async (request: PageRequest) => {
            const start = request.cursor ? Number(request.cursor) : 0;
            const limit = request.cursor ? 100 : 50;
            const end = Math.min(rows.length, start + limit);
            return {
                items: rows.slice(start, end),
                next_cursor: end < rows.length ? String(end) : null,
            };
        });
        let state = await loadPrefix(emptyFolderPages, fetch, 50);
        expect([state.loaded, state.total]).toEqual([50, 51]);
        let request = pageRequest(state, 0)!;
        expect(request.cursor).toBe("50");
        state = applyPage(state, request, await fetch(request));
        expect([state.loaded, state.total]).toEqual([150, 151]);
        request = pageRequest(state, 1)!;
        expect(request.cursor).toBe("150");
        state = applyPage(state, request, await fetch(request));
        expect([state.loaded, state.total, state.cursor]).toEqual([
            180,
            180,
            null,
        ]);
        expect(folderComplete(state)).toBe(true);
    });
});
