import type { Directory, DriveItem } from "./types";

/** Coalesce invalidations without cancelling a listing that is still loading. */
export function createListingRefreshQueue(load: () => Promise<void>) {
  let running = false;
  let pending = false;
  let disposed = false;
  const finish = () => {
    running = false;
    if (pending && !disposed) {
      pending = false;
      request();
    }
  };
  const request = () => {
    if (disposed) return;
    if (running) {
      pending = true;
      return;
    }
    running = true;
    // The caller presents load errors. A failed request must still release the
    // queue so a later upload notification can retry the listing.
    void Promise.resolve()
      .then(() => {
        if (!disposed) return load();
      })
      .then(finish, finish);
  };
  return {
    request,
    dispose() {
      disposed = true;
      pending = false;
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
  if (unchanged && previous.name === next.name) return previous;
  return {
    ...next,
    contents: unchanged ? previous.contents : Object.fromEntries(entries),
  };
}
