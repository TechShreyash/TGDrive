import type { Transfer } from "./transfers";
import { isArchiveTransfer, isServerImport } from "./transferTasks";

function isDeviceTransfer(item: Transfer): boolean {
    return (
        !isServerImport(item) &&
        !isArchiveTransfer(item) &&
        item.kind !== "delete" &&
        !item.historyOnly &&
        item.initiatedHere === true
    );
}

/** Device progress includes handed-off files, independent of their Telegram stage. */
export function deviceUploadProgress(items: Transfer[]) {
    const files = items.filter(
        (item) => isDeviceTransfer(item) && item.state !== "cancelled",
    );
    const pending = files.filter(needsDeviceUpload);
    const sizes = files.map((item) => item.size);
    const known = sizes.every((size) => Number.isFinite(size) && size >= 0);
    const total = known
        ? sizes.reduce((sum, size) => sum + size, 0)
        : undefined;
    const current = files.reduce((sum, item) => {
        if (!Number.isFinite(item.size) || item.size < 0) return sum;
        if (!needsDeviceUpload(item)) return sum + item.size;
        const observed =
            item.state === "receiving" && Number.isFinite(item.sentCurrent)
                ? Math.max(item.current, item.sentCurrent!)
                : item.current;
        return (
            sum +
            (Number.isFinite(observed)
                ? Math.max(0, Math.min(observed, item.size))
                : 0)
        );
    }, 0);
    return {
        count: files.length,
        received: files.length - pending.length,
        pending: pending.length > 0,
        current,
        total,
        fraction:
            total && total > 0
                ? Math.min(pending.length ? 0.99 : 1, current / total)
                : undefined,
    };
}

/** A job ID confirms that the server owns the file and its remaining work. */
export function needsDeviceUpload(item: Transfer): boolean {
    return (
        isDeviceTransfer(item) &&
        !item.jobId &&
        item.state !== "completed" &&
        item.state !== "cancelled"
    );
}

export function deviceUploadMessage(
    items: Transfer[],
    otherDeviceUploadsPending = false,
): string | null {
    const deviceItems = items.filter(
        (item) => isDeviceTransfer(item) && item.state !== "cancelled",
    );
    // Finished receipts and server-discovered transfers need no device reminder.
    if (
        !deviceItems.length ||
        deviceItems.every((item) => item.state === "completed")
    )
        return null;

    const pending = deviceItems.filter(needsDeviceUpload).length;
    if (pending) {
        return "Keep this tab open";
    }

    if (otherDeviceUploadsPending)
        return "Keep this tab open for other uploads";

    if (deviceItems.some((item) => item.state === "error"))
        return "You can close this tab";

    return "You can close this tab";
}
