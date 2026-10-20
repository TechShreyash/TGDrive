import { api, ApiError, errorMessage, request } from "./api";
import type { Job, UploadSnapshot, TransferLocation } from "./types";
import { sampleProgress, type ProgressSample } from "./progress";
import { sendUploadChunk, type UploadChunkTransport } from "./uploadTransport";
export type TransferState =
  | "queued"
  | "verifying"
  | "receiving"
  | "awaiting_file"
  | "uploading"
  | "cancelling"
  | "completed"
  | "error"
  | "cancelled";
export interface Transfer {
  key: string;
  name: string;
  size: number;
  lastModified: number;
  destination: string;
  relativePath?: string;
  batchId?: string;
  batchName?: string;
  batchCreatedAt?: number;
  state: TransferState;
  phase: string;
  current: number;
  /** Transient browser-observed bytes; current remains the confirmed resume offset. */
  sentCurrent?: number;
  total: number;
  uploadId?: string;
  jobId?: string;
  kind?: string;
  error?: string;
  file?: File;
  controller?: AbortController;
  createdAt?: number;
  startedAt?: number;
  finishedAt?: number;
  meter?: ProgressSample;
  stage?: string;
  serverStatus?: string;
  speedBytesPerSecond?: number | null;
  etaSeconds?: number | null;
  progressUpdatedAt?: number | null;
  serverObservedAt?: number;
  historyOnly?: boolean;
  /** Browser-only receipt: server-discovered jobs never open this device's popup. */
  initiatedHere?: boolean;
  location?: TransferLocation;
}
const STORAGE_KEY = "tgdrive.uploads.v2";
export const MAX_BROWSER_QUEUE = 2000;
export const BROWSER_RECEIVE_CONCURRENCY = 1;
const MAX_HISTORY_BATCHES = 30;
const MAX_HISTORY_ROWS = 2000;
export interface UploadEntry {
  file: File;
  destination: string;
  relativePath?: string;
}
const terminal = new Set<TransferState>(["completed", "cancelled", "error"]);
export function safeTransferMetadata(transfers: Transfer[]) {
  return transfers.map(
    ({
      key,
      name,
      size,
      lastModified,
      destination,
      relativePath,
      batchId,
      batchName,
      batchCreatedAt,
      state,
      phase,
      current,
      total,
      uploadId,
      jobId,
      kind,
      initiatedHere,
      createdAt,
      startedAt,
      finishedAt,
    }) => ({
      key,
      name,
      size,
      lastModified,
      destination,
      relativePath,
      batchId,
      batchName,
      batchCreatedAt,
      state,
      phase,
      current,
      total,
      uploadId,
      jobId,
      kind,
      initiatedHere,
      createdAt,
      startedAt,
      finishedAt,
    }),
  );
}
export function transferBatchId(item: Transfer): string {
  return item.batchId || "legacy:" + (item.jobId || item.key);
}
export function retainTransferBatches(items: Transfer[]): Transfer[] {
  const groups = new Map<string, Transfer[]>();
  for (const item of items) {
    const id = transferBatchId(item);
    const batch = groups.get(id) || [];
    batch.push(item);
    groups.set(id, batch);
  }
  const keep = new Set<string>();
  const finished: { id: string; rows: Transfer[]; time: number }[] = [];
  for (const [id, rows] of groups) {
    if (rows.some((item) => !["completed", "cancelled"].includes(item.state)))
      keep.add(id);
    else
      finished.push({
        id,
        rows,
        time: Math.max(
          ...rows.map(
            (item) =>
              item.finishedAt || item.batchCreatedAt || item.createdAt || 0,
          ),
        ),
      });
  }
  finished.sort((a, b) => b.time - a.time);
  let historyRows = 0;
  for (const batch of finished.slice(0, MAX_HISTORY_BATCHES)) {
    if (historyRows + batch.rows.length > MAX_HISTORY_ROWS) continue;
    keep.add(batch.id);
    historyRows += batch.rows.length;
  }
  return items.filter((item) => keep.has(transferBatchId(item)));
}
export async function verifyReceivedChunks(
  file: File,
  chunks: NonNullable<UploadSnapshot["checksums"]>,
  offset: number,
  progress: (bytes: number) => void = () => {},
  signal?: AbortSignal,
): Promise<void> {
  let checked = 0;
  for (const chunk of [...chunks].sort((a, b) => a.offset - b.offset)) {
    if (signal?.aborted) throw new DOMException("Upload paused", "AbortError");
    if (chunk.offset >= offset) break;
    if (
      chunk.offset !== checked ||
      chunk.size <= 0 ||
      chunk.offset + chunk.size > offset
    )
      throw new Error(
        "The server has incomplete resume information. Start a new upload.",
      );
    const digest = await crypto.subtle.digest(
      "SHA-256",
      await file.slice(chunk.offset, chunk.offset + chunk.size).arrayBuffer(),
    );
    const hash = [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    if (hash !== chunk.sha256)
      throw new Error(
        "This file differs from the original upload. Choose the original file or cancel and start again.",
      );
    if (signal?.aborted) throw new DOMException("Upload paused", "AbortError");
    checked += chunk.size;
    progress(checked);
  }
  if (checked !== offset)
    throw new Error(
      "The server has incomplete resume information. Start a new upload.",
    );
}
export function uploadBackupState(
  items: Transfer[],
): "pending" | "attention" | null {
  const unfinished = items.filter(
    (item) =>
      item.kind !== "delete" &&
      !["completed", "cancelled"].includes(item.state),
  );
  if (
    unfinished.some((item) => ["error", "awaiting_file"].includes(item.state))
  )
    return "attention";
  return unfinished.length ? "pending" : null;
}
export function canCancelTransfer(item: Transfer): boolean {
  return (
    !item.historyOnly &&
    item.kind !== "delete" &&
    item.stage !== "backup_pending" &&
    !["completed", "cancelled"].includes(item.state)
  );
}
export function fromJob(job: Job, previousKind?: string): Partial<Transfer> {
  const state: TransferState =
    job.status === "completed"
      ? "completed"
      : job.status === "cancelled"
        ? "cancelled"
        : job.status === "error" ||
            job.status === "failed" ||
            job.status === "interrupted"
          ? "error"
          : job.status === "cancelling" || job.status === "cancel_requested"
            ? "cancelling"
            : "uploading";
  let phase =
    state === "error"
      ? job.kind === "delete"
        ? "Deletion failed. Retry to finish cleanup"
        : job.phase === "backup_pending"
          ? "Backup Needs Attention. Retry to secure this file"
          : "Upload failed"
      : state === "cancelled"
        ? "Cancelled"
        : state === "completed"
          ? job.kind === "delete"
            ? "Permanently deleted"
            : "Available in your drive"
          : state === "cancelling"
            ? "Cancelling and cleaning up"
            : job.kind === "delete"
              ? "Deleting from Telegram"
              : job.phase === "backup_pending"
                ? "Securing your drive backup in Telegram"
                : job.phase === "retry_wait"
                  ? "Waiting for Telegram; retry scheduled"
                  : job.phase === "downloading"
                    ? "Downloading from URL"
                    : job.phase === "finalizing"
                      ? "Publishing to the channel"
                      : job.status === "queued"
                        ? "Queued for Telegram"
                        : "Sending data to Telegram";
  if (
    state === "uploading" &&
    job.status === "running" &&
    ["uploading", "finalizing"].includes(job.phase) &&
    job.telegram_parts &&
    job.telegram_parts.total > 1
  ) {
    const { stored, total } = job.telegram_parts;
    phase += " · " + stored + "/" + total + " Parts in Channel";
  }
  if (job.queue && job.queue.position > 0) {
    phase += " · Queue position " + job.queue.position;
    if (job.queue.estimated_wait_seconds != null)
      phase +=
        " · about " + Math.ceil(job.queue.estimated_wait_seconds) + "s wait";
  }
  return {
    state,
    phase,
    ...(job.location ? { location: job.location } : {}),
    ...(previousKind === "remote"
      ? { kind: "remote" }
      : job.kind
        ? { kind: job.kind }
        : {}),
    current: job.current ?? 0,
    total: job.total ?? 0,
    error: job.error || undefined,
    stage: job.phase,
    serverStatus: job.status,
    speedBytesPerSecond: job.speed_bytes_per_second,
    etaSeconds: job.eta_seconds,
    progressUpdatedAt: job.progress_updated_at,
    serverObservedAt: Date.now(),
    ...(job.created_at ? { createdAt: job.created_at * 1000 } : {}),
    ...(job.started_at ? { startedAt: job.started_at * 1000 } : {}),
    ...(job.finished_at ? { finishedAt: job.finished_at * 1000 } : {}),
  };
}
export class TransferManager {
  recoveryWarning = "";
  private items: Transfer[] = [];
  private deletionJobs: Job[] = [];
  private listeners = new Set<() => void>();
  private enabled = false;
  private admissionPaused = false;
  private receiving = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private polling = false;
  private refreshRequested = false;
  private capacityKnown = false;
  private stagingRetryAt = 0;
  private dismissed = new Set<string>();
  constructor(
    private readonly sendChunk: UploadChunkTransport = sendUploadChunk,
  ) {
    try {
      this.dismissed = new Set(
        JSON.parse(localStorage.getItem("tgdrive.dismissed-jobs.v1") || "[]"),
      );
    } catch {}
    try {
      const saved: Transfer[] = JSON.parse(
        localStorage.getItem(STORAGE_KEY) || "[]",
      );
      this.items = saved
        .filter(
          (item) =>
            item.kind !== "delete" &&
            // Rediscover server-owned rows from the live registry. Keeping
            // their browser copies can resurrect jobs after a drive/fixture reset.
            item.initiatedHere !== false &&
            (item.initiatedHere || !item.batchId?.startsWith("server:")) &&
            typeof item.key === "string" &&
            typeof item.name === "string" &&
            typeof item.destination === "string" &&
            Number.isFinite(item.size),
        )
        .map((item) => ({
          ...safeTransferMetadata([item])[0],
          initiatedHere:
            item.initiatedHere ??
            Boolean(
              item.uploadId ||
              (item.batchId && !/^(server:|history:)/.test(item.batchId)),
            ),
          batchId: transferBatchId(item),
          batchName: item.batchName || item.name,
          batchCreatedAt: item.batchCreatedAt || item.createdAt || Date.now(),
          state: item.jobId
            ? item.state
            : terminal.has(item.state)
              ? item.state
              : item.kind === "remote"
                ? "error"
                : "awaiting_file",
          phase: item.jobId
            ? item.phase
            : terminal.has(item.state)
              ? item.phase
              : item.kind === "remote"
                ? "URL transfer is missing its server job. Start a new URL upload."
                : "Choose the original file to resume",
        }));
    } catch {
      this.recoveryWarning =
        "Your browser could not load transfer recovery information. Keep this tab open until device transfers finish.";
    }
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  snapshot = () => this.items;
  deletionSnapshot = () => this.deletionJobs;
  refreshJobs = () => {
    if (this.polling) this.refreshRequested = true;
    else void this.poll();
  };
  async retryDeletion(id: string) {
    if (
      !this.deletionJobs.some((job) => job.id === id && job.kind === "delete")
    )
      return;
    await request("/api/v1/jobs/" + encodeURIComponent(id) + "/retry", {});
    await this.poll();
  }
  private publish(persist = true) {
    if (!persist) {
      this.items = [...this.items];
      for (const listener of this.listeners) listener();
      return;
    }
    const retained = retainTransferBatches(this.items);
    const retainedKeys = new Set(retained.map((item) => item.key));
    for (const item of this.items)
      if (item.jobId && !retainedKeys.has(item.key))
        this.dismissed.add(item.jobId);
    this.items = [...retained];
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify(safeTransferMetadata(this.items)),
      );
      localStorage.setItem(
        "tgdrive.dismissed-jobs.v1",
        JSON.stringify([...this.dismissed].slice(-1000)),
      );
      this.recoveryWarning = "";
    } catch {
      this.recoveryWarning =
        "Your browser could not save transfer recovery information. Keep this tab open until device transfers finish.";
    }
    for (const listener of this.listeners) listener();
  }
  private update(key: string, values: Partial<Transfer>) {
    const item = this.items.find((item) => item.key === key);
    if (item) {
      Object.assign(item, values);
      if (item.state !== "receiving") item.sentCurrent = undefined;
      if (["receiving", "verifying"].includes(item.state))
        item.startedAt ??= Date.now();
      if (values.state && !terminal.has(values.state))
        item.finishedAt = undefined;
      if (terminal.has(item.state)) item.finishedAt ??= Date.now();
      if (item.state === "receiving")
        item.meter = sampleProgress(
          item.meter,
          item.state,
          item.sentCurrent ?? item.current,
          Date.now(),
        );
      else item.meter = undefined;
      this.publish(Object.keys(values).some((key) => key !== "sentCurrent"));
    }
  }
  setEnabled(enabled: boolean) {
    if (this.enabled === enabled) return;
    this.enabled = enabled;
    this.capacityKnown = false;
    if (!enabled) {
      clearTimeout(this.timer);
      for (const item of this.items) item.controller?.abort();
      return;
    }
    void this.poll();
    void this.startNext();
  }
  setAdmissionPaused(paused: boolean) {
    if (this.admissionPaused === paused) return;
    this.admissionPaused = paused;
    if (!paused) void this.startNext();
  }
  canQueue(count: number): boolean {
    const unfinished = new Set(
      this.items
        .filter((item) => !["completed", "cancelled"].includes(item.state))
        .map(transferBatchId),
    );
    const retainedRows = this.items.filter((item) =>
      unfinished.has(transferBatchId(item)),
    ).length;
    return (
      Number.isSafeInteger(count) &&
      count >= 0 &&
      retainedRows + count <= MAX_BROWSER_QUEUE
    );
  }
  add(files: File[], destination: string, maxSize: number): string[] {
    return this.addEntries(
      files.map((file) => ({ file, destination })),
      maxSize,
    );
  }
  addEntries(
    entries: UploadEntry[],
    maxSize: number,
    requireAll = false,
    batch?: { name: string; id?: string; createdAt?: number },
  ): string[] {
    const rejected: string[] = [];
    const accepted = entries.filter(({ file }) => {
      if (
        !Number.isSafeInteger(file.size) ||
        !file.size ||
        file.size > maxSize
      ) {
        rejected.push(
          file.name +
            (file.size
              ? ": exceeds the upload limit"
              : ": empty files are not supported"),
        );
        return false;
      }
      return true;
    });
    if (requireAll && rejected.length) return rejected;
    if (!this.canQueue(accepted.length))
      return [
        "The upload queue can track up to 2,000 files in unfinished batches. Finish or cancel an existing batch, then try again.",
      ];
    if (!accepted.length) return rejected;
    const batchId = batch?.id || crypto.randomUUID();
    const createdAt = batch?.createdAt || Date.now();
    const batchName =
      batch?.name ||
      (accepted.length === 1
        ? accepted[0].file.name
        : accepted.length + " files");
    for (const { file, destination, relativePath } of accepted)
      this.items.push({
        key: crypto.randomUUID(),
        initiatedHere: true,
        createdAt: Date.now(),
        batchId,
        batchName,
        batchCreatedAt: createdAt,
        relativePath,
        name: file.name,
        size: file.size,
        lastModified: file.lastModified,
        destination,
        state: "queued",
        phase: "Waiting to send from this device",
        current: 0,
        total: file.size,
        file,
      });
    this.publish();
    void this.startNext();
    return rejected;
  }
  addJob(jobId: string, name: string, destination: string, kind = "upload") {
    if (kind === "delete") {
      this.refreshJobs();
      return;
    }
    this.items.push({
      key: crypto.randomUUID(),
      initiatedHere: true,
      createdAt: Date.now(),
      name,
      batchId: crypto.randomUUID(),
      batchName: name,
      batchCreatedAt: Date.now(),
      size: 0,
      lastModified: 0,
      destination,
      jobId,
      kind,
      state: "uploading",
      phase: "Queued on server",
      current: 0,
      total: 0,
    });
    this.publish();
    void this.poll();
  }
  async resume(key: string, file: File) {
    const item = this.items.find((item) => item.key === key);
    if (!item) return;
    if (item.kind === "remote")
      throw new Error(
        "URL uploads resume on the server. Retry the transfer instead.",
      );
    if (
      file.name.normalize("NFC") !== item.name.normalize("NFC") ||
      file.size !== item.size
    )
      throw new Error(
        "Choose the same file name and size as the original upload.",
      );
    this.update(key, {
      file,
      state: "queued",
      error: undefined,
      phase: "Waiting to check the original file",
    });
    void this.startNext();
  }
  async resumeBatchFiles(batchId: string, files: File[]) {
    const pending = this.items.filter(
      (item) =>
        transferBatchId(item) === batchId &&
        !item.jobId &&
        (item.state === "awaiting_file" || item.state === "error"),
    );
    const folderBatch = pending.some((item) => Boolean(item.relativePath));
    const fileKey = (file: File) =>
      folderBatch
        ? "path:" + (file.webkitRelativePath || "").normalize("NFC")
        : JSON.stringify([
            file.name.normalize("NFC"),
            file.size,
            file.lastModified,
          ]);
    const itemKey = (item: Transfer) =>
      folderBatch
        ? "path:" + (item.relativePath || "").normalize("NFC")
        : JSON.stringify([
            item.name.normalize("NFC"),
            item.size,
            item.lastModified,
          ]);
    const selected = new Map<string, File[]>();
    for (const file of files) {
      const key = fileKey(file);
      selected.set(key, [...(selected.get(key) || []), file]);
    }
    const expected = new Map<string, number>();
    for (const item of pending) {
      const key = itemKey(item);
      expected.set(key, (expected.get(key) || 0) + 1);
    }
    let matched = 0;
    const errors: string[] = [];
    for (const item of pending) {
      const key = itemKey(item);
      const candidates = selected.get(key);
      if (!candidates?.length) continue;
      if (candidates.length !== 1 || expected.get(key) !== 1) {
        errors.push(
          "More than one file matches " +
            (item.relativePath || item.name) +
            ". Choose that file individually.",
        );
        continue;
      }
      const file = candidates[0];
      if (
        file.name.normalize("NFC") !== item.name.normalize("NFC") ||
        file.size !== item.size
      ) {
        errors.push(
          (item.relativePath || item.name) +
            ": the name or size differs from the original upload.",
        );
        continue;
      }
      Object.assign(item, {
        file,
        state: "queued",
        error: undefined,
        finishedAt: undefined,
        phase: "Waiting to check the original file",
      });
      matched += 1;
    }
    if (matched) {
      this.publish();
      void this.startNext();
    }
    return {
      matched,
      remaining: pending.length - matched,
      unmatched: files.length - matched,
      errors,
    };
  }
  async retry(key: string) {
    const item = this.items.find((item) => item.key === key);
    if (!item) return;
    if (item.jobId) {
      const payload = await request<{ id: string }>(
        "/api/v1/jobs/" + encodeURIComponent(item.jobId) + "/retry",
        {},
      );
      this.update(key, {
        jobId: payload.id,
        state: "uploading",
        error: undefined,
        phase: "Queued for retry",
      });
      void this.poll();
    } else if (item.file) {
      this.update(key, {
        state: "queued",
        error: undefined,
        phase: "Waiting to resume",
      });
      void this.startNext();
    } else if (item.kind === "remote")
      throw new Error(
        "This URL transfer has no server job. Start a new URL upload.",
      );
    else
      this.update(key, {
        state: "awaiting_file",
        error: undefined,
        phase: "Choose the original file to resume",
      });
  }
  async cancel(key: string) {
    const item = this.items.find((item) => item.key === key);
    if (!item || !canCancelTransfer(item)) return;
    this.update(key, {
      state: "cancelling",
      phase: "Cancelling and cleaning up",
      error: undefined,
    });
    item.controller?.abort();
    try {
      if (item.jobId) {
        const result = await api.cancelJob(item.jobId);
        if (result.data) this.update(key, fromJob(result.data, item.kind));
      } else {
        if (item.uploadId) await api.cancelUpload(item.uploadId);
        this.update(key, {
          state: "cancelled",
          phase: "Cancelled",
          file: undefined,
        });
      }
    } catch (error) {
      if (error instanceof ApiError && error.code === "BACKUP_PENDING") {
        this.update(key, {
          state: "uploading",
          stage: "backup_pending",
          error: undefined,
          phase: "Securing your drive backup in Telegram",
        });
        void this.poll();
        return;
      }
      this.update(key, {
        state: "cancelling",
        error: errorMessage(error),
        phase: "Cancellation not yet confirmed. Try Again.",
      });
      throw error;
    } finally {
      void this.startNext();
    }
  }
  clearFinished() {
    for (const item of this.items)
      if (item.jobId && ["completed", "cancelled"].includes(item.state))
        this.dismissed.add(item.jobId);
    try {
      localStorage.setItem(
        "tgdrive.dismissed-jobs.v1",
        JSON.stringify([...this.dismissed].slice(-1000)),
      );
    } catch {}
    const unfinished = new Set(
      this.items
        .filter((item) => !["completed", "cancelled"].includes(item.state))
        .map(transferBatchId),
    );
    this.items = this.items.filter((item) =>
      unfinished.has(transferBatchId(item)),
    );
    this.publish();
  }
  private async cancelItems(items: Transfer[]) {
    for (const item of items) {
      item.state = "cancelling";
      item.phase = "Cancelling and cleaning up";
      item.controller?.abort();
    }
    this.publish();
    const errors: unknown[] = [];
    for (const item of items) {
      try {
        await this.cancel(item.key);
      } catch (error) {
        errors.push(error);
      }
    }
    if (errors.length)
      throw new Error(
        "Some cancellations are not confirmed. Check the affected files and try again.",
      );
  }
  async cancelBatch(batchId: string) {
    await this.cancelItems(
      this.items.filter(
        (item) => transferBatchId(item) === batchId && canCancelTransfer(item),
      ),
    );
  }
  async retryBatch(batchId: string) {
    for (const item of this.items.filter(
      (item) => transferBatchId(item) === batchId && item.state === "error",
    ))
      await this.retry(item.key);
  }
  async cancelAll(keys?: string[]) {
    const selected = keys ? new Set(keys) : null;
    await this.cancelItems(
      this.items.filter(
        (item) =>
          canCancelTransfer(item) && (!selected || selected.has(item.key)),
      ),
    );
  }
  private async startNext() {
    if (
      !this.enabled ||
      this.admissionPaused ||
      this.receiving ||
      !this.capacityKnown ||
      Date.now() < this.stagingRetryAt
    )
      return;
    const item = this.items.find(
      (item) => item.state === "queued" && item.file,
    );
    if (!item) return;
    this.receiving = true;
    try {
      await this.receive(item);
    } catch (error) {
      if (
        item.state !== "cancelling" &&
        item.state !== "cancelled" &&
        error instanceof ApiError &&
        [429, 507].includes(error.status)
      ) {
        this.stagingRetryAt =
          Date.now() + Math.max(5000, error.retryAfter * 1000);
        this.update(item.key, {
          state: "queued",
          phase: "Waiting for server space",
          error: undefined,
        });
      } else if (item.state !== "cancelling" && item.state !== "cancelled")
        this.update(item.key, {
          state:
            error instanceof DOMException && error.name === "AbortError"
              ? "awaiting_file"
              : "error",
          phase: "Upload paused",
          error: errorMessage(error),
        });
    } finally {
      item.controller = undefined;
      this.receiving = false;
      void this.startNext();
    }
  }
  private async receive(item: Transfer) {
    const file = item.file!;
    const controller = new AbortController();
    item.controller = controller;
    this.update(item.key, {
      state: "receiving",
      stage: "preparing",
      phase: "Preparing device upload",
      sentCurrent: undefined,
    });
    if (!item.uploadId) {
      const created = await api.createUpload(
        item.name,
        item.destination,
        item.size,
      );
      this.update(item.key, { uploadId: created.id });
    }
    if (item.state === "cancelling" || controller.signal.aborted) {
      if (item.uploadId) await api.cancelUpload(item.uploadId);
      return;
    }
    const id = item.uploadId!;
    const head = await fetch("/api/v1/uploads/" + encodeURIComponent(id), {
      method: "HEAD",
      credentials: "same-origin",
      signal: controller.signal,
    });
    if (!head.ok) {
      if (head.status === 401)
        window.dispatchEvent(new Event("tgdrive:auth-required"));
      throw new ApiError(
        head.status === 404 || head.status === 410
          ? "This upload has expired. Cancel it and start a new upload."
          : "Could not check the upload position. Try Again.",
        head.status,
      );
    }
    const offsetHeader = head.headers.get("Upload-Offset");
    const lengthHeader = head.headers.get("Upload-Length");
    let offset =
      offsetHeader === null || offsetHeader === "" ? NaN : Number(offsetHeader);
    const length =
      lengthHeader === null || lengthHeader === "" ? NaN : Number(lengthHeader);
    if (
      !Number.isSafeInteger(offset) ||
      offset < 0 ||
      offset > file.size ||
      !Number.isSafeInteger(length) ||
      length !== file.size
    )
      throw new Error("The server returned an invalid upload position.");
    const snapshot = await api.upload(id);
    if (offset) {
      this.update(item.key, {
        state: "verifying",
        phase: "Checking the original file before resuming",
        current: offset,
        total: file.size,
      });
      await verifyReceivedChunks(
        file,
        snapshot.checksums || [],
        offset,
        undefined, // Hashing must not replace acknowledged upload progress.
        controller.signal,
      );
    }
    const chunkSize = snapshot.chunk_size;
    if (
      !Number.isSafeInteger(chunkSize) ||
      chunkSize <= 0 ||
      chunkSize > 32 * 1024 * 1024
    )
      throw new Error("The server returned an invalid chunk size.");
    this.update(item.key, {
      state: "receiving",
      phase: "Sending from this device",
      stage: "sending",
      current: offset,
      sentCurrent: offset,
      total: file.size,
      error: undefined,
    });
    while (offset < file.size) {
      if (controller.signal.aborted)
        throw new DOMException("Upload paused", "AbortError");
      const end = Math.min(offset + chunkSize, file.size);
      const response = await this.sendChunk(
        "/api/v1/uploads/" + encodeURIComponent(id),
        {
          method: "PATCH",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/octet-stream",
            "Upload-Offset": String(offset),
          },
          body: file.slice(offset, end),
          signal: controller.signal,
        },
        (sent) => {
          if (controller.signal.aborted || item.state !== "receiving") return;
          this.update(item.key, {
            sentCurrent: Math.max(item.sentCurrent ?? offset, offset + sent),
            ...(offset + sent >= file.size
              ? {
                  stage: "confirming",
                  phase: "Sent from device. Waiting for server confirmation",
                }
              : {}),
          });
        },
      );
      if (!response.ok) {
        if (response.status === 401)
          window.dispatchEvent(new Event("tgdrive:auth-required"));
        let detail = "";
        try {
          const failure = await response.json();
          if (typeof failure.detail === "string")
            detail = failure.detail.slice(0, 500);
        } catch {
          /* A proxy may return a non-JSON error page. */
        }
        throw new ApiError(
          detail ||
            (response.status === 409
              ? "The upload position changed. Retry to safely resume."
              : response.status === 507
                ? "Temporary Storage is full. Free space or cancel a transfer, then retry."
                : "Sending this file was interrupted. Retry to resume."),
          response.status,
        );
      }
      const nextOffset = Number(response.headers.get("Upload-Offset"));
      if (nextOffset !== end)
        throw new Error(
          "The server did not confirm this file chunk. Retry to check the upload position.",
        );
      offset = nextOffset;
      this.update(item.key, { current: offset, sentCurrent: offset });
    }
    if (controller.signal.aborted) return;
    this.update(item.key, {
      phase: "Receipt complete. Preparing Telegram upload",
      stage: "confirming",
    });
    const result = await api.completeUpload(id);
    this.update(item.key, {
      jobId: result.id,
      state: "uploading",
      phase: "Queued for Telegram",
      current: 0,
      total: file.size,
      file: undefined,
    });
    void this.poll();
  }
  private async poll() {
    if (!this.enabled || this.polling) return;
    clearTimeout(this.timer);
    this.polling = true;
    try {
      const payload = await api.jobs();
      this.capacityKnown = true;
      const previousDeletions = new Map(
        this.deletionJobs.map((job) => [job.id, job.status]),
      );
      this.deletionJobs = payload.jobs.filter((job) => job.kind === "delete");
      if (
        this.deletionJobs.some(
          (job) =>
            job.status === "completed" &&
            previousDeletions.get(job.id) !== "completed",
        )
      )
        window.dispatchEvent(new Event("tgdrive:files-changed"));
      for (const item of this.items)
        if (
          item.state === "queued" &&
          item.phase === "Waiting for server connection"
        ) {
          item.phase = "Waiting to send from this device";
          item.error = undefined;
        }
      for (const job of payload.jobs) {
        // Deletions are a single file-operation notice, never upload transfers.
        if (job.kind === "delete" || this.dismissed.has(job.id)) continue;
        let item = this.items.find(
          (item) => item.jobId === job.id || item.uploadId === job.id,
        );
        if (!item) {
          item = {
            key: crypto.randomUUID(),
            name: job.name || "Server transfer",
            batchId: "server:" + job.id,
            batchName: job.name || "Server transfer",
            batchCreatedAt: job.created_at ? job.created_at * 1000 : Date.now(),
            size: job.total || 0,
            lastModified: 0,
            destination: job.destination || "/",
            jobId: job.id,
            state: "uploading",
            phase: "Queued",
            current: 0,
            total: job.total,
          };
          this.items.push(item);
        }
        if (!item.jobId) {
          item.jobId = job.id;
          item.file = undefined;
        }
        const wasComplete = item.state === "completed";
        const createdAt = item.createdAt;
        const startedAt = item.startedAt;
        Object.assign(item, fromJob(job, item.kind));
        if (startedAt)
          item.startedAt = Math.min(startedAt, item.startedAt || startedAt);
        if (createdAt)
          item.createdAt = Math.min(createdAt, item.createdAt || createdAt);
        if (terminal.has(item.state)) item.finishedAt ??= Date.now();
        else item.finishedAt = undefined;
        item.meter = undefined;
        if (!wasComplete && item.state === "completed")
          window.dispatchEvent(new Event("tgdrive:files-changed"));
      }
      this.publish();
    } catch {
      this.capacityKnown = false;
      for (const item of this.items)
        if (item.jobId && !terminal.has(item.state))
          item.error = "Progress connection lost. Trying again…";
        else if (item.state === "queued") {
          item.phase = "Waiting for server connection";
          item.error = "Could not check server queue capacity. Trying again…";
        }
      this.publish();
    } finally {
      this.polling = false;
      if (this.refreshRequested && this.enabled) {
        this.refreshRequested = false;
        void this.poll();
        return;
      }
      this.refreshRequested = false;
      if (this.enabled) void this.startNext();
      if (this.enabled)
        this.timer = setTimeout(
          () => {
            void this.poll();
          },
          this.items.some((item) => item.jobId && !terminal.has(item.state)) ||
            this.deletionJobs.some(
              (job) =>
                ![
                  "completed",
                  "cancelled",
                  "error",
                  "failed",
                  "interrupted",
                ].includes(job.status),
            )
            ? 1500
            : 10000,
        );
    }
  }
}
export const transfers = new TransferManager();
export function isTransferActive(item: Transfer) {
  return !terminal.has(item.state);
}
