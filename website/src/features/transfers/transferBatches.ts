import type { Transfer } from "./transfers";
import { currentRate, transferDetails, transferProgress } from "./progress";
import { deviceUploadProgress, needsDeviceUpload } from "./deviceUploadStatus";
import type { BatchProgressMeters } from "./batchProgress";
import { isFolderTransfer, isServerImport, transferTaskId } from "./transferTasks";

export interface TransferBatch {
    id: string;
    name: string;
    folder: boolean;
    items: Transfer[];
    count: number;
    completed: number;
    cancelled: number;
    failed: number;
    awaiting: number;
    active: number;
    queued: number;
    pending: boolean;
    unfinished: number;
    device: ReturnType<typeof deviceUploadProgress>;
    displayBytes: number;
    displayRate?: number;
    displayEta?: number;
    totalBytes?: number;
    completedBytes: number;
    uploadedBytes: number;
    progress?: number;
    elapsed?: number;
    deviceRate?: number;
    telegramRate?: number;
    eta?: number;
}

function positive(value: number | undefined | null): value is number {
    return typeof value === "number" && Number.isFinite(value) && value > 0;
}
function bounded(value: number, maximum: number): number {
    return Number.isFinite(value) ? Math.max(0, Math.min(value, maximum)) : 0;
}
function isQueued(item: Transfer): boolean {
    return (
        item.state === "queued" ||
        (item.state === "uploading" &&
            (item.stage === "queued" || item.serverStatus === "queued"))
    );
}
function finished(item: Transfer): boolean {
    return item.state === "completed" || item.state === "cancelled";
}
function sizeOf(item: Transfer): number | undefined {
    if (item.kind === "delete") return;
    if (positive(item.size)) return item.size;
    if ((item.jobId || isServerImport(item)) && positive(item.total))
        return item.total;
}
function priority(item: Transfer): number {
    if (item.state === "error") return 0;
    if (item.state === "awaiting_file") return 1;
    if (!finished(item)) return isQueued(item) ? 3 : 2;
    return item.state === "completed" ? 4 : 5;
}
function timestamps(items: Transfer[]): number[] {
    return items
        .flatMap((item) => [item.batchCreatedAt, item.createdAt])
        .filter(positive);
}
function elapsed(items: Transfer[], now: number): number | undefined {
    const starts = items.map((item) => item.startedAt).filter(positive);
    if (!starts.length) return;
    let end = now;
    if (items.every(finished)) {
        const ends = items.map((item) => item.finishedAt).filter(positive);
        if (ends.length !== items.length) return;
        end = Math.max(...ends);
    }
    return Math.max(0, (end - Math.min(...starts)) / 1000);
}
/** Received device bytes are not Telegram upload bytes. */
function telegramBytes(item: Transfer, size: number): number {
    if (item.state === "completed") return size;
    if (
        !item.jobId ||
        isQueued(item) ||
        ["cancelled", "error", "awaiting_file"].includes(item.state) ||
        ["saving", "verifying", "downloading"].includes(item.stage || "")
    )
        return 0;
    return bounded(item.current, size);
}
function progress(
    items: Transfer[],
    total: number | undefined,
    uploadedBytes: number,
): number | undefined {
    if (items.length === 1) return transferProgress(items[0]).fraction;
    if (items.every((item) => item.state === "completed")) return 1;
    if (
        !positive(total) ||
        items.some((item) => isServerImport(item) || item.kind === "delete")
    )
        return;
    return Math.min(0.99, uploadedBytes / total);
}
function summarize(
    id: string,
    source: Transfer[],
    now: number,
    meters?: BatchProgressMeters,
): TransferBatch {
    const items = [...source].sort((a, b) => priority(a) - priority(b));
    const sizes = items.map(sizeOf);
    const allSizes = sizes.every(positive);
    const sum = sizes.reduce<number>((total, size) => total + (size ?? 0), 0);
    const totalBytes = allSizes && positive(sum) ? sum : undefined;
    let completedBytes = 0;
    let uploadedBytes = 0;
    let deviceRate = 0;
    let telegramRate = 0;
    for (let index = 0; index < items.length; index++) {
        const item = items[index];
        if (item.state === "completed") completedBytes += sizes[index] ?? 0;
        const size = sizes[index];
        if (positive(size)) uploadedBytes += telegramBytes(item, size);
        const rate = transferDetails(item, now).rate;
        if (!positive(rate) || item.error || isQueued(item)) continue;
        if (item.state === "receiving" || item.state === "verifying")
            deviceRate += rate;
        else if (
            item.state === "uploading" &&
            ["uploading", "torrent_uploading"].includes(item.stage || "") &&
            item.kind !== "delete"
        )
            telegramRate += rate;
    }
    const completed = items.filter((item) => item.state === "completed").length;
    const cancelled = items.filter((item) => item.state === "cancelled").length;
    const failed = items.filter((item) => item.state === "error").length;
    const awaiting = items.filter(
        (item) => item.state === "awaiting_file",
    ).length;
    const queued = items.filter(isQueued).length;
    const active =
        items.length - completed - cancelled - failed - awaiting - queued;
    const remaining = items.filter((item) => !finished(item));
    const device = deviceUploadProgress(items);
    const deviceRemaining =
        device.total !== undefined
            ? Math.max(0, device.total - device.current)
            : 0;
    const deviceUnblocked = items
        .filter(needsDeviceUpload)
        .every(
            (item) =>
                !item.error &&
                !["error", "awaiting_file", "cancelling"].includes(
                    item.state,
                ) &&
                !["retry_wait", "waiting_for_telegram"].includes(
                    item.stage || "",
                ),
        );
    const pendingTelegramBytes = remaining.some(
        (item) =>
            item.jobId &&
            item.state === "uploading" &&
            !item.error &&
            ["uploading", "queued"].includes(item.stage || "") &&
            positive(sizeOf(item)) &&
            (isQueued(item) || item.current < sizeOf(item)!),
    );
    const meter = meters?.get(id);
    const observedRate = currentRate(meter, now);
    if (
        device.pending &&
        deviceUnblocked &&
        positive(deviceRemaining) &&
        meter?.stage === "device" &&
        !positive(deviceRate)
    )
        deviceRate = observedRate ?? 0;
    else if (
        !device.pending &&
        pendingTelegramBytes &&
        meter?.stage === "telegram" &&
        !positive(telegramRate)
    )
        telegramRate = observedRate ?? 0;
    let eta: number | undefined;
    if (
        remaining.every((item) => positive(sizeOf(item))) &&
        positive(telegramRate) &&
        remaining.length &&
        remaining.every(
            (item) =>
                item.jobId &&
                item.state === "uploading" &&
                !item.error &&
                !isServerImport(item) &&
                item.kind !== "delete" &&
                (["uploading", "queued"].includes(item.stage || "") ||
                    (["finalizing", "backup_pending"].includes(
                        item.stage || "",
                    ) &&
                        bounded(item.current, sizeOf(item)!) >= sizeOf(item)!)),
        )
    ) {
        const bytes = remaining.reduce((sum, item) => {
            const size = sizeOf(item)!;
            return (
                sum + size - (isQueued(item) ? 0 : bounded(item.current, size))
            );
        }, 0);
        const estimate = bytes / telegramRate;
        if (positive(estimate)) eta = estimate;
    }
    const deviceEstimate =
        positive(deviceRate) && positive(deviceRemaining) && deviceUnblocked
            ? deviceRemaining / deviceRate
            : undefined;
    const deviceEta = positive(deviceEstimate) ? deviceEstimate : undefined;
    return {
        id,
        name:
            source.find((item) => item.folderUpload)?.folderUpload?.name ||
            source[0].name ||
            "Transfer",
        folder: source.some(isFolderTransfer),
        items,
        count: items.length,
        completed,
        cancelled,
        failed,
        awaiting,
        active,
        queued,
        pending: items.some((item) => !finished(item)),
        unfinished: items.filter(
            (item) => !finished(item) && item.state !== "error",
        ).length,
        device,
        displayBytes: device.pending ? device.current : uploadedBytes,
        displayRate: device.pending
            ? positive(deviceRate)
                ? deviceRate
                : undefined
            : positive(telegramRate)
              ? telegramRate
              : undefined,
        displayEta: device.pending ? deviceEta : eta,
        totalBytes,
        completedBytes,
        uploadedBytes,
        progress: device.pending
            ? device.fraction
            : progress(items, totalBytes, uploadedBytes),
        elapsed: elapsed(items, now),
        deviceRate: positive(deviceRate) ? deviceRate : undefined,
        telegramRate: positive(telegramRate) ? telegramRate : undefined,
        eta,
    };
}

/** Pure aggregation: no browser storage, timers, mutations, or provider requests. */
export function summarizeBatches(
    items: Transfer[],
    now: number,
    meters?: BatchProgressMeters,
): TransferBatch[] {
    const groups = new Map<string, Transfer[]>();
    for (const item of items) {
        const id = transferTaskId(item);
        const group = groups.get(id) || [];
        group.push(item);
        groups.set(id, group);
    }
    const order = (batch: TransferBatch) =>
        batch.device.pending
            ? batch.failed || batch.awaiting
                ? 0
                : 1
            : batch.failed || batch.awaiting
              ? 2
              : batch.pending
                ? 3
                : 4;
    const created = (batch: TransferBatch) => {
        const starts = timestamps(batch.items);
        return starts.length ? Math.min(...starts) : 0;
    };
    return [...groups]
        .map(([id, group]) => summarize(id, group, now, meters))
        .sort(
            (a, b) =>
                order(a) - order(b) ||
                created(b) - created(a) ||
                a.id.localeCompare(b.id),
        );
}

/** A batch belongs to one category; child file states never split its identity. */
export function batchCategory(
    batch: TransferBatch,
): "running" | "queued" | "error" | "completed" | "cancelled" {
    if (batch.active > 0) return "running";
    if (batch.queued > 0) return "queued";
    if (batch.failed + batch.awaiting > 0) return "error";
    return batch.completed === batch.count ? "completed" : "cancelled";
}
