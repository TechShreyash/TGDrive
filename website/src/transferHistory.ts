import { summarizeBatches, batchCategory } from "./transferBatches";
import type { Job } from "./types";
import { fromJob, type Transfer } from "./transfers";

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
  if (item.state === "error" || item.state === "awaiting_file") return "error";
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
  includeLocalFinished = true,
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
      .filter((batch) => ["running", "queued"].includes(batchCategory(batch)))
      .flatMap((batch) => batch.items.map((item) => item.key)),
  );
  for (const item of local) {
    if (item.kind !== "delete") {
      const key = item.jobId || item.uploadId || item.key;
      const historical = records.get(key);
      if (
        (!includeLocalFinished || item.jobId) &&
        !historical &&
        !liveKeys.has(item.key)
      )
        continue;
      records.set(
        key,
        historical?.location
          ? { ...item, location: historical.location }
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
      item.state === "completed"
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
): Transfer[] {
  const rows = mergeHistory(jobs, local, "all", firstPage);
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
