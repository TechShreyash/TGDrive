import type { DriveItem, NodePage } from "../../shared/types";
import { reconcileDriveItems } from "./listingUpdates";

export const PAGE_SIZE = 100;
const FIRST_LIMIT = 200;
const MAX_LIMIT = 500;

/** A folder listing cached by absolute position under one sort and filter. */
export interface FolderPages {
    /** Sparse: pages reached by offset leave holes before them. */
    rows: (DriveItem | undefined)[];
    total: number;
    /** End of the contiguous prefix that `cursor` continues. */
    loaded: number;
    cursor: string | null;
    folder?: NodePage["folder"];
}
export interface PageRequest {
    limit: number;
    cursor?: string;
    offset?: number;
    around?: string;
}
export type FetchPage = (request: PageRequest) => Promise<NodePage>;

export const emptyFolderPages: FolderPages = {
    rows: [],
    total: 0,
    loaded: 0,
    cursor: null,
};

export const folderComplete = (state: FolderPages) =>
    state.loaded >= state.total;

export function pageLoaded(
    rows: FolderPages["rows"],
    total: number,
    page: number,
): boolean {
    const end = Math.min(total, (page + 1) * PAGE_SIZE);
    for (let index = page * PAGE_SIZE; index < end; index++)
        if (rows[index] === undefined) return false;
    return true;
}

/** The request that fills a wanted page, or null when it is already loaded. */
export function pageRequest(
    state: FolderPages,
    page: number,
): PageRequest | null {
    if (pageLoaded(state.rows, state.total, page)) return null;
    const start = page * PAGE_SIZE;
    const end = Math.min(state.total, start + PAGE_SIZE);
    // Continuing the cursor is one cheap indexed read; offsets skip entries.
    if (state.cursor && state.loaded >= start && state.loaded < end)
        return { cursor: state.cursor, limit: MAX_LIMIT };
    return { offset: start, limit: PAGE_SIZE };
}

export function applyPage(
    state: FolderPages,
    request: PageRequest,
    page: NodePage,
): FolderPages {
    // A refresh replaced the prefix while this cursor page was in flight.
    if (request.cursor !== undefined && request.cursor !== state.cursor)
        return state;
    const start =
        request.cursor !== undefined
            ? state.loaded
            : request.around !== undefined
              ? (page.index ?? 0)
              : (request.offset ?? 0);
    const rows = state.rows.slice();
    page.items.forEach((item, index) => (rows[start + index] = item));
    const end = start + page.items.length;
    const extended = start <= state.loaded && end > state.loaded;
    return {
        rows,
        total: page.total,
        loaded: extended ? end : state.loaded,
        cursor: extended ? page.next_cursor : state.cursor,
        folder: page.folder ?? state.folder,
    };
}

/** Load the first rows, or reload as many as `previous` had contiguously. */
export async function loadPrefix(
    previous: FolderPages,
    fetch: FetchPage,
): Promise<FolderPages> {
    const target = Math.max(previous.loaded, FIRST_LIMIT);
    let state = emptyFolderPages;
    let request: PageRequest | null = {
        offset: 0,
        limit: Math.min(target, MAX_LIMIT),
    };
    while (request) {
        state = applyPage(state, request, await fetch(request));
        request =
            state.cursor && state.loaded < target
                ? {
                      cursor: state.cursor,
                      limit: Math.min(MAX_LIMIT, target - state.loaded),
                  }
                : null;
    }
    return state;
}

/** Drop rows (such as deleted items); later rows move up into their places. */
export function removeRows(
    state: FolderPages,
    drop: (item: DriveItem) => boolean,
): FolderPages {
    const rows: FolderPages["rows"] = [];
    let loaded = state.loaded;
    // A plain loop: forEach skips the holes that offset pages leave.
    for (let index = 0; index < state.rows.length; index++) {
        const item = state.rows[index];
        if (!item || !drop(item)) rows.push(item);
        else if (index < state.loaded) loaded--;
    }
    const removed = state.rows.length - rows.length;
    return removed
        ? { ...state, rows, loaded, total: state.total - removed }
        : state;
}

/** Keep unchanged rows and the folder record so refreshes do not churn the UI. */
export function reconcilePages(
    previous: FolderPages,
    next: FolderPages,
): FolderPages {
    const rows = reconcileDriveItems(
        previous.rows.filter((item): item is DriveItem => item !== undefined),
        next.rows as DriveItem[],
    );
    const folder =
        JSON.stringify(previous.folder) === JSON.stringify(next.folder)
            ? previous.folder
            : next.folder;
    const same =
        folder === previous.folder &&
        next.total === previous.total &&
        next.loaded === previous.loaded &&
        next.cursor === previous.cursor &&
        rows.length === previous.rows.length &&
        rows.every((item, index) => previous.rows[index] === item);
    return same ? previous : { ...next, rows, folder };
}
