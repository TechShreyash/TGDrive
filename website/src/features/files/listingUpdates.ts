import type { Directory, DriveItem } from "../../shared/types";

/** Coalesce invalidations without cancelling a listing that is still loading. */
export function createListingRefreshQueue(load: () => Promise<void | boolean>) {
    let running = false;
    let disposed = false;
    let current: ((success: boolean) => void)[] = [];
    let queued: ((success: boolean) => void)[] = [];
    const start = () => {
        running = true;
        current = queued;
        queued = [];
        const finish = (success: boolean) => {
            for (const resolve of current) resolve(success && !disposed);
            current = [];
            running = false;
            if (queued.length && !disposed) start();
        };
        // The caller presents load errors. A failed request must still release the
        // queue so a later upload notification can retry the listing.
        void Promise.resolve()
            .then(() => {
                return disposed ? false : load();
            })
            .then(
                (result) => finish(result !== false),
                () => finish(false),
            );
    };
    const request = (): Promise<boolean> => {
        if (disposed) return Promise.resolve(false);
        return new Promise((resolve) => {
            queued.push(resolve);
            if (!running) start();
        });
    };
    return {
        request,
        dispose() {
            disposed = true;
            for (const resolve of [...current, ...queued]) resolve(false);
            current = [];
            queued = [];
        },
    };
}

function sameItem(previous: DriveItem, next: DriveItem): boolean {
    const keys = Object.keys(next) as (keyof DriveItem)[];
    if (Object.keys(previous).length !== keys.length) return false;
    return keys.every((key) => {
        const before = previous[key];
        const after = next[key];
        return (
            before === after ||
            (Array.isArray(before) &&
                Array.isArray(after) &&
                before.length === after.length &&
                before.every((value, index) => value === after[index]))
        );
    });
}

export function reconcileDriveItems(
    previous: DriveItem[],
    next: DriveItem[],
): DriveItem[] {
    const byId = new Map(previous.map((item) => [item.id, item]));
    const reconciled = next.map((item) => {
        const before = byId.get(item.id);
        return before && sameItem(before, item) ? before : item;
    });
    return previous.length === reconciled.length &&
        reconciled.every((item, index) => previous[index] === item)
        ? previous
        : reconciled;
}

/** Preserve unchanged rows so background refreshes do not churn their UI. */
export function reconcileDirectory(
    previous: Directory,
    next: Directory,
): Directory {
    const entries = Object.entries(next.contents).map(([id, item]) => {
        const before = previous.contents[id];
        return [id, before && sameItem(before, item) ? before : item] as const;
    });
    const unchanged =
        Object.keys(previous.contents).length === entries.length &&
        entries.every(([id, item]) => previous.contents[id] === item);
    if (
        unchanged &&
        previous.name === next.name &&
        previous.read_only === next.read_only
    )
        return previous;
    return {
        ...next,
        contents: unchanged ? previous.contents : Object.fromEntries(entries),
    };
}
