import { summarizeBatches, batchCategory } from "./transferBatches";
import type { Job } from "../../shared/types";
import { fromJob, type Transfer } from "./transfers";
import { api, type TransferHistoryContext } from "../../shared/api";
import { isArchiveTransfer } from "./transferTasks";
import type { TransferTaskHistoryPage } from "../../shared/types";

export const TRANSFER_TASK_PAGE_SIZE = 20;

export type HistoryFilter =
    | "all"
    | "queued"
    | "running"
    | "finished"
    | "completed"
    | "error"
    | "cancelled";
export function historyStatus(
    item: Transfer,
): Exclude<HistoryFilter, "all" | "finished"> {
    if (item.state === "completed" || item.state === "cancelled")
        return item.state;
    if (item.state === "error" || item.state === "awaiting_file")
        return "error";
    if (
        item.state === "queued" ||
        (item.state === "uploading" &&
            (item.serverStatus === "queued" || item.stage === "queued"))
    )
        return "queued";
    return "running";
}
export function mergeHistory(
    jobs: Job[],
    local: Transfer[],
    filter: HistoryFilter,
    includeLocalFinished: boolean | "selected" = true,
): Transfer[] {
    const records = new Map<string, Transfer>();
    for (const job of jobs) {
        if (job.kind === "delete") continue;
        records.set(job.id, {
            key: "history:" + job.id,
            name: job.name || "Transfer",
            size: job.total || 0,
            total: job.total || 0,
            current: 0,
            lastModified: 0,
            destination: job.destination || "/",
            batchId: "history:" + job.id,
            batchName: job.name || "Transfer",
            batchCreatedAt: job.created_at ? job.created_at * 1000 : undefined,
            jobId: job.id,
            state: "uploading",
            phase: "Loading transfer",
            ...fromJob(job),
            historyOnly: true,
        });
    }
    // Browser receipts have not reached the server job list yet; live records
    // also replace stale history samples with current progress/retry state.
    const liveKeys = new Set(
        summarizeBatches(local, Date.now())
            .filter((batch) =>
                ["running", "queued"].includes(batchCategory(batch)),
            )
            .flatMap((batch) => batch.items.map((item) => item.key)),
    );
    for (const item of local) {
        if (item.kind !== "delete") {
            const key = item.jobId || item.uploadId || item.key;
            const historical = records.get(key);
            if (
                includeLocalFinished !== "selected" &&
                (!includeLocalFinished || item.jobId) &&
                !historical &&
                !liveKeys.has(item.key)
            )
                continue;
            records.set(
                key,
                historical?.location || historical?.folderUpload
                    ? {
                          ...item,
                          ...(historical.location
                              ? { location: historical.location }
                              : {}),
                          ...(historical.folderUpload && !item.folderUpload
                              ? { folderUpload: historical.folderUpload }
                              : {}),
                      }
                    : item,
            );
        }
    }
    const all = [...records.values()];
    const matching = new Set(
        summarizeBatches(all, Date.now())
            .filter(
                (batch) =>
                    filter === "all" ||
                    (filter === "finished"
                        ? !["running", "queued"].includes(batchCategory(batch))
                        : batchCategory(batch) === filter),
            )
            .flatMap((batch) => batch.items.map((item) => item.key)),
    );
    return all
        .filter((item) => matching.has(item.key))
        .map((item) =>
            item.state === "completed" && !isArchiveTransfer(item)
                ? { ...item, phase: "Upload completed" }
                : item,
        );
}

/** Keep current batches above the selected terminal history page. */
export function visibleHistory(
    jobs: Job[],
    local: Transfer[],
    filter: HistoryFilter,
    firstPage: boolean,
    selectedLocalTasks = false,
): Transfer[] {
    const rows = mergeHistory(
        jobs,
        local,
        "all",
        selectedLocalTasks ? "selected" : firstPage,
    );
    const matching = new Set(
        summarizeBatches(rows, Date.now())
            .filter((batch) => {
                const category = batchCategory(batch);
                return (
                    category === "running" ||
                    category === "queued" ||
                    filter === "all" ||
                    category === filter
                );
            })
            .flatMap((batch) => batch.items.map((item) => item.key)),
    );
    return rows.filter((item) => matching.has(item.key));
}

/** Search keeps every child of a matching folder together. */
export function matchHistoryTasks(
    items: Transfer[],
    query: string,
): Transfer[] {
    const term = query.normalize("NFC").toLocaleLowerCase().trim();
    if (!term) return items;
    return summarizeBatches(items, Date.now())
        .filter((task) =>
            [task.name, ...task.items.map((item) => item.name)].some((name) =>
                name.normalize("NFC").toLocaleLowerCase().includes(term),
            ),
        )
        .flatMap((task) => task.items);
}

export function liveHistoryRows(items: Transfer[]): Transfer[] {
    return summarizeBatches(items, Date.now())
        .filter((task) => ["running", "queued"].includes(batchCategory(task)))
        .flatMap((task) => task.items);
}

/** Merge browser-owned finished tasks into the same 20-task page budget. */
export async function loadTaskHistoryPage(
    local: Transfer[],
    filter: HistoryFilter,
    page: number,
    signal: AbortSignal,
    query = "",
    onLocalItems?: (items: Transfer[]) => void,
): Promise<TransferTaskHistoryPage & { localItems: Transfer[] }> {
    const tasks = summarizeBatches(local, Date.now());
    const context: TransferHistoryContext = { exclude_folders: [] };
    for (const task of tasks) {
        if (
            task.folder &&
            task.items.some((item) => item.jobId || item.uploadId) &&
            task.items.some((item) => !item.jobId)
        )
            context.exclude_folders!.push(task.id);
    }
    const matchingKeys = new Set(
        matchHistoryTasks(local, query).map((item) => item.key),
    );
    const localFinished = tasks
        .filter((task) => {
            const category = batchCategory(task);
            return (
                task.items.some((item) => !item.jobId && !item.historyOnly) &&
                !["running", "queued"].includes(category) &&
                (filter === "all" ||
                    filter === "finished" ||
                    filter === category) &&
                task.items.some((item) => matchingKeys.has(item.key))
            );
        })
        .sort((a, b) => {
            const created = (task: typeof a) =>
                Math.min(
                    ...task.items.map(
                        (item) => item.batchCreatedAt || item.createdAt || 0,
                    ),
                );
            return created(b) - created(a) || a.id.localeCompare(b.id);
        });
    const start = (page - 1) * TRANSFER_TASK_PAGE_SIZE;
    const localPage = localFinished.slice(
        start,
        start + TRANSFER_TASK_PAGE_SIZE,
    );
    onLocalItems?.(localPage.flatMap((task) => task.items));
    context.offset = Math.max(0, start - localFinished.length);
    const result = await api.transferTaskHistory(
        filter === "all" ? "finished" : filter,
        page,
        signal,
        query,
        context,
    );
    const selectedIds = new Set<string>();
    const capacity = TRANSFER_TASK_PAGE_SIZE - localPage.length;
    for (const job of result.jobs) {
        const id = job.folder_upload
            ? "folder:" + job.folder_upload.id
            : "job:" + job.id;
        if (selectedIds.has(id) || selectedIds.size < capacity)
            selectedIds.add(id);
    }
    const jobs = result.jobs.filter((job) =>
        selectedIds.has(
            job.folder_upload
                ? "folder:" + job.folder_upload.id
                : "job:" + job.id,
        ),
    );
    const jobIds = new Set(jobs.map((job) => job.id));
    const localItems = [
        ...localPage.flatMap((task) => task.items),
        ...local.filter((item) =>
            jobIds.has(item.jobId || item.uploadId || ""),
        ),
    ];
    const total = result.total_tasks + localFinished.length;
    const totalPages = Math.max(1, Math.ceil(total / TRANSFER_TASK_PAGE_SIZE));
    return {
        ...result,
        jobs,
        localItems,
        page: Math.min(page, totalPages),
        page_size: TRANSFER_TASK_PAGE_SIZE,
        total_tasks: total,
        total_pages: totalPages,
        has_more: start + TRANSFER_TASK_PAGE_SIZE < total,
    };
}
