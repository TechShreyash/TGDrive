import { summarizeBatches, batchCategory } from "./transferBatches";
import type { Job } from "./types";
import { fromJob, type Transfer } from "./transfers";

export type HistoryFilter =
  "all" | "queued" | "running" | "completed" | "error" | "cancelled";
export function historyStatus(item: Transfer): Exclude<HistoryFilter, "all"> {
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
  for (const item of local) {
    if (item.kind !== "delete") {
      const key = item.jobId || item.uploadId || item.key;
      const historical = records.get(key);
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
      .filter((batch) => filter === "all" || batchCategory(batch) === filter)
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
