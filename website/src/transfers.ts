import {
  api,
  ApiError,
  errorMessage,
  notifyDemoFilesReadOnly,
  request,
} from "./api";
import type {
  Job,
  UploadSnapshot,
  TransferLocation,
  FolderUploadMetadata,
} from "./types";
import { transferTaskId } from "./transferTasks";
import { sampleProgress, type ProgressSample } from "./progress";
import { BatchProgressTracker } from "./batchProgress";
import { summarizeBatches } from "./transferBatches";
import { sendUploadChunk, type UploadChunkTransport } from "./uploadTransport";
import {
  DeviceUploadConcurrency,
  INITIAL_DEVICE_UPLOAD_CONCURRENCY,
  MAX_DEVICE_UPLOAD_CONCURRENCY,
} from "./deviceUploadConcurrency";
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
  folderUpload?: FolderUploadMetadata;
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
  remoteRequestId?: string;
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
// Device receipts have their own slots; Telegram work never holds one.
export const BROWSER_RECEIVE_CONCURRENCY = MAX_DEVICE_UPLOAD_CONCURRENCY;
export const BROWSER_RECEIVE_INITIAL_CONCURRENCY =
  INITIAL_DEVICE_UPLOAD_CONCURRENCY;
const MAX_HISTORY_BATCHES = 30;
const MAX_HISTORY_ROWS = 2000;
// Receipt verification/handoff is control work, independent of payload slots.
const RECEIPT_COMPLETION_CONCURRENCY = 2;
const DEVICE_RETRY_DELAYS_MS = [2000, 4000, 8000];
const TRANSIENT_DEVICE_STATUSES = new Set([0, 408, 500, 502, 503, 504]);
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
      folderUpload,
      batchId,
      batchName,
      batchCreatedAt,
      state,
      phase,
      current,
      total,
      uploadId,
      jobId,
      remoteRequestId,
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
      folderUpload,
      batchId,
      batchName,
      batchCreatedAt,
      state,
      phase,
      current,
      total,
      uploadId,
      jobId,
      remoteRequestId,
      kind,
      initiatedHere,
      createdAt,
      startedAt,
      finishedAt,
    }),
  );
}
export function transferBatchId(item: Transfer): string {
  return transferTaskId(item);
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
                  : ["checking_source", "downloading"].includes(job.phase) &&
                      job.download_mode === "waiting_storage"
                    ? "Waiting for temporary storage"
                    : job.phase === "checking_source" &&
                        job.download_mode === "queued"
                      ? "Waiting for a source connection"
                      : job.phase === "checking_source"
                        ? job.status === "queued"
                          ? "Waiting to check source"
                          : "Checking source…"
                        : job.phase === "downloading"
                          ? job.download_mode === "warm"
                            ? "Queued · receiving slowly"
                            : "Downloading from URL"
                          : job.phase === "finalizing"
                            ? "Publishing to the channel"
                            : job.status === "queued"
                              ? "Queued for Telegram"
                              : "Uploading to Telegram";
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
    ...(job.folder_upload
      ? {
          folderUpload: job.folder_upload,
          batchId: job.folder_upload.id,
          batchName: job.folder_upload.name,
          batchCreatedAt: job.folder_upload.created_at * 1000,
        }
      : {}),
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
export interface DeletionProgress {
  jobs: Job[];
  completed: number;
  error: string;
}

const deletionTerminal = new Set([
  "completed",
  "cancelled",
  "error",
  "failed",
  "interrupted",
]);

export class TransferManager {
  recoveryWarning = "";
  private items: Transfer[] = [];
  private deletionJobs: Job[] = [];
  private noticedDeletions = new Set<string>();
  private deletionVersion = 0;
  private deletionUpdates = new Map<string, number>();
  private deletionProgress: DeletionProgress = {
    jobs: [],
    completed: 0,
    error: "",
  };
  private listeners = new Set<() => void>();
  private batchProgress = new BatchProgressTracker();
  private enabled = false;
  private admissionPaused = false;
  private receiving = new Set<string>();
  private processing = new Set<string>();
  private lastReceivingBatch: string | undefined;
  private completing = 0;
  private completionQueue: (() => void)[] = [];
  private deviceConcurrency = new DeviceUploadConcurrency();
  private concurrencyTimer: ReturnType<typeof setInterval> | undefined;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private stagingTimer: ReturnType<typeof setTimeout> | undefined;
  private polling = false;
  private refreshRequested = false;
  private stagingRetries = new Map<string, number>();
  private deviceRetryAttempts = new Map<string, number>();
  private admissionRetryAt = 0;
  private dismissed = new Set<string>();
  private historyRevision = 0;
  private remoteStarts = new Map<
    string,
    {
      url: string;
      destination: string;
      singleThreaded: boolean;
      requestId: string;
    }
  >();
  private remoteSubmitting = new Set<string>();
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
          batchName: item.folderUpload?.name || item.batchName || item.name,
          batchCreatedAt:
            (item.folderUpload ? item.folderUpload.created_at * 1000 : 0) ||
            item.batchCreatedAt ||
            item.createdAt ||
            Date.now(),
          state:
            item.jobId || (item.kind === "remote" && item.remoteRequestId)
              ? item.state
              : terminal.has(item.state)
                ? item.state
                : item.kind === "remote"
                  ? "error"
                  : "awaiting_file",
          phase:
            item.kind === "remote" &&
            item.remoteRequestId &&
            !item.jobId &&
            !terminal.has(item.state)
              ? "Checking import status…"
              : item.jobId
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
  batchProgressSnapshot = () => this.batchProgress.snapshot();
  deletionSnapshot = () => this.deletionProgress;
  private updateDeletionProgress(error = "") {
    this.deletionProgress = {
      jobs: this.deletionJobs,
      completed: this.deletionJobs.filter(
        (job) =>
          job.status === "completed" && this.noticedDeletions.has(job.id),
      ).length,
      error,
    };
  }
  trackDeletions(jobs: Pick<Job, "id" | "name">[]) {
    if (!jobs.length) return;
    if (!this.deletionJobs.some((job) => !deletionTerminal.has(job.status)))
      this.noticedDeletions.clear();
    for (const job of jobs) {
      const queued: Job = {
        ...job,
        kind: "delete",
        status: "queued",
        phase: "queued",
        current: 0,
        total: 0,
      };
      this.deletionJobs = [
        ...this.deletionJobs.filter((old) => old.id !== job.id),
        queued,
      ];
      this.noticedDeletions.add(job.id);
      this.deletionUpdates.set(job.id, ++this.deletionVersion);
    }
    this.updateDeletionProgress();
    this.publish(false);
    this.refreshJobs();
  }
  dismissDeletionResult = () => {
    for (const job of this.deletionJobs)
      if (job.status === "completed") this.noticedDeletions.delete(job.id);
    this.updateDeletionProgress(this.deletionProgress.error);
    this.publish(false);
  };
  refreshJobs = () => {
    if (this.polling) this.refreshRequested = true;
    else void this.poll();
  };
  async retryDeletion(id: string) {
    if (
      !this.deletionJobs.some((job) => job.id === id && job.kind === "delete")
    )
      return;
    const job = await request<Job>(
      "/api/v1/jobs/" + encodeURIComponent(id) + "/retry",
      {},
    );
    this.deletionJobs = [
      ...this.deletionJobs.filter((old) => old.id !== id),
      job,
    ];
    this.noticedDeletions.add(id);
    this.deletionUpdates.set(id, ++this.deletionVersion);
    this.updateDeletionProgress();
    this.publish(false);
    // A poll started before this retry must be followed by a fresh request.
    this.refreshJobs();
  }
  private publish(persist = true) {
    const now = Date.now();
    if (this.enabled && !this.admissionPaused)
      this.batchProgress.observe(summarizeBatches(this.items, now), now);
    else this.batchProgress.clear();
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
    if (!enabled) {
      this.batchProgress.clear();
      clearTimeout(this.timer);
      clearTimeout(this.stagingTimer);
      clearInterval(this.concurrencyTimer);
      this.concurrencyTimer = undefined;
      this.deviceConcurrency.pauseMeasurements(Date.now());
      for (const item of this.items) item.controller?.abort();
      return;
    }
    void this.poll();
    void this.startNext();
  }
  setAdmissionPaused(paused: boolean) {
    if (this.admissionPaused === paused) return;
    this.admissionPaused = paused;
    if (paused) this.batchProgress.clear();
    this.deviceConcurrency.pauseMeasurements(Date.now());
    this.updateConcurrencyTimer();
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
    batch?: {
      name: string;
      id?: string;
      createdAt?: number;
      folderUpload?: FolderUploadMetadata;
    },
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
    const folder = batch?.folderUpload;
    const folderSelection = Boolean(
      folder || accepted.some((entry) => entry.relativePath),
    );
    const batchId = folder?.id || batch?.id || crypto.randomUUID();
    const createdAt = folder
      ? folder.created_at * 1000
      : batch?.createdAt || Date.now();
    for (const { file, destination, relativePath } of accepted) {
      const key = crypto.randomUUID();
      this.items.push({
        key,
        initiatedHere: true,
        createdAt: Date.now(),
        batchId: folderSelection ? batchId : key,
        batchName: folderSelection
          ? folder?.name || batch?.name || file.name
          : file.name,
        batchCreatedAt: createdAt,
        relativePath,
        ...(folder ? { folderUpload: folder } : {}),
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
    }
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
  startRemote(
    url: string,
    destination: string,
    singleThreaded: boolean,
    requestId: string,
  ): string {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol))
      throw new Error("Enter a direct HTTP or HTTPS file link.");
    if (!this.canQueue(1))
      throw new Error(
        "Finish or cancel an existing transfer before adding more.",
      );
    const key = crypto.randomUUID();
    let name = parsed.pathname.split("/").at(-1) || "URL import";
    try {
      name = decodeURIComponent(name);
    } catch {
      /* Keep the URL basename. */
    }
    const now = Date.now();
    this.items.push({
      key,
      name,
      destination,
      batchId: key,
      batchName: name,
      batchCreatedAt: now,
      createdAt: now,
      initiatedHere: true,
      size: 0,
      lastModified: 0,
      current: 0,
      total: 0,
      kind: "remote",
      remoteRequestId: requestId,
      state: "uploading",
      stage: "checking_source",
      phase: "Starting URL import…",
    });
    this.remoteStarts.set(key, { url, destination, singleThreaded, requestId });
    this.publish();
    void this.submitRemote(key);
    return key;
  }
  private acceptRemote(key: string, result: { id: string; data?: Job }) {
    const item = this.items.find((row) => row.key === key);
    if (!item) return;
    if (result.id) {
      item.jobId = result.id;
      // Polling may discover this server job before its start response arrives.
      this.items = this.items.filter(
        (row) => row.key === key || row.jobId !== result.id,
      );
      this.remoteStarts.delete(key);
    }
    if (result.data && item.state !== "cancelled")
      Object.assign(item, fromJob(result.data, "remote"));
    else if (!["cancelled", "cancelling"].includes(item.state)) {
      item.state = "uploading";
      item.phase = "Checking source…";
      item.stage = "checking_source";
      item.error = undefined;
    }
    this.publish();
  }
  private async recoverRemote(key: string, originalError?: unknown) {
    const item = this.items.find((row) => row.key === key);
    if (!item?.remoteRequestId) return;
    try {
      const result = await api.remoteRequest(item.remoteRequestId);
      if (!result.id && result.data.status === "queued")
        throw new Error(
          "The server is still accepting this import. Retry to check it.",
        );
      this.acceptRemote(key, result);
    } catch (error) {
      if (["cancelled", "cancelling"].includes(item.state)) return;
      this.update(key, {
        state: "error",
        stage: "checking_source",
        phase: "Import needs attention",
        error: errorMessage(originalError || error),
      });
    }
  }
  private async submitRemote(key: string) {
    const pending = this.remoteStarts.get(key);
    if (!pending || this.remoteSubmitting.has(key)) return;
    this.remoteSubmitting.add(key);
    try {
      this.acceptRemote(
        key,
        await api.remote(
          pending.url,
          pending.destination,
          pending.singleThreaded,
          pending.requestId,
        ),
      );
    } catch (error) {
      // A lost response must not silently create a second import on retry.
      await this.recoverRemote(key, error);
    } finally {
      this.remoteSubmitting.delete(key);
      void this.poll();
    }
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
    this.deviceRetryAttempts.delete(key);
    this.stagingRetries.delete(key);
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
      this.deviceRetryAttempts.delete(item.key);
      this.stagingRetries.delete(item.key);
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
      this.deviceRetryAttempts.delete(key);
      this.stagingRetries.delete(key);
      this.update(key, {
        state: "queued",
        error: undefined,
        phase: "Waiting to resume",
      });
      void this.startNext();
    } else if (item.kind === "remote") {
      this.update(key, {
        state: "uploading",
        error: undefined,
        phase: "Checking import status…",
      });
      if (this.remoteStarts.has(key)) await this.submitRemote(key);
      else if (item.remoteRequestId) await this.recoverRemote(key);
      else
        throw new Error(
          "This URL transfer has no server job. Start a new URL upload.",
        );
    } else
      this.update(key, {
        state: "awaiting_file",
        error: undefined,
        phase: "Choose the original file to resume",
      });
  }
  async cancel(key: string) {
    const item = this.items.find((item) => item.key === key);
    if (!item || !canCancelTransfer(item)) return;
    this.stagingRetries.delete(item.key);
    this.deviceRetryAttempts.delete(item.key);
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
      } else if (item.kind === "remote" && item.remoteRequestId) {
        this.acceptRemote(
          key,
          await api.cancelRemoteRequest(item.remoteRequestId),
        );
        this.remoteStarts.delete(key);
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
  async clearHistory() {
    const unfinished = new Set(
      this.items
        .filter(
          (item) => !["completed", "cancelled", "error"].includes(item.state),
        )
        .map(transferBatchId),
    );
    const removed = this.items.filter(
      (item) =>
        item.kind !== "delete" && !unfinished.has(transferBatchId(item)),
    );
    const keys = new Set(removed.map((item) => item.key));
    const excludeFolders = [
      ...new Set(
        this.items
          .filter((item) => unfinished.has(transferBatchId(item)))
          .flatMap((item) => (item.folderUpload ? [item.folderUpload.id] : [])),
      ),
    ];
    await api.clearTransferHistory(excludeFolders);
    this.historyRevision++;
    this.items = this.items.filter((item) => !keys.has(item.key));
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
  private startNext() {
    try {
      if (!this.enabled || this.admissionPaused) return;
      clearTimeout(this.stagingTimer);
      if (Date.now() < this.admissionRetryAt) {
        this.stagingTimer = setTimeout(
          () => this.startNext(),
          this.admissionRetryAt - Date.now(),
        );
        return;
      }
      this.startCompletions();
      while (this.receiving.size < this.deviceConcurrency.limit) {
        const eligible = this.items.filter(
          (item) =>
            item.state === "queued" &&
            item.file &&
            !this.processing.has(item.key) &&
            Date.now() >= (this.stagingRetries.get(item.key) || 0),
        );
        if (!eligible.length) break;
        // Give each selection/folder a turn. A newly added folder can use the
        // next free device slot without waiting behind an older folder's tail.
        const batches = [...new Set(eligible.map(transferBatchId))];
        const previous = batches.indexOf(this.lastReceivingBatch || "");
        const nextBatch = batches[(previous + 1) % batches.length];
        const item = eligible.find(
          (candidate) => transferBatchId(candidate) === nextBatch,
        );
        if (!item) break;
        this.lastReceivingBatch = transferBatchId(item);
        this.stagingRetries.delete(item.key);
        this.receiving.add(item.key);
        this.processing.add(item.key);
        void this.receiveNext(item);
      }
      const retryTimes = this.items
        .filter((item) => item.state === "queued" && item.file)
        .map((item) => this.stagingRetries.get(item.key) || 0)
        .filter((at) => at > Date.now());
      if (retryTimes.length)
        this.stagingTimer = setTimeout(
          () => this.startNext(),
          Math.max(0, Math.min(...retryTimes) - Date.now()),
        );
    } finally {
      this.updateConcurrencyTimer();
    }
  }
  private canMeasureDeviceUpload() {
    return (
      this.enabled &&
      !this.admissionPaused &&
      (typeof document === "undefined" || document.visibilityState !== "hidden")
    );
  }
  private sampleDeviceConcurrency() {
    const now = Date.now();
    if (!this.canMeasureDeviceUpload()) {
      this.deviceConcurrency.pauseMeasurements(now);
      return;
    }
    const previous = this.deviceConcurrency.limit;
    const hasBacklog =
      now >= this.admissionRetryAt &&
      this.items.some(
        (item) =>
          item.state === "queued" &&
          item.file &&
          !this.processing.has(item.key) &&
          now >= (this.stagingRetries.get(item.key) || 0),
      );
    this.deviceConcurrency.sample(now, hasBacklog);
    if (previous !== this.deviceConcurrency.limit) this.startNext();
  }
  private updateConcurrencyTimer() {
    if (this.enabled && !this.admissionPaused && this.receiving.size) {
      this.concurrencyTimer ??= setInterval(
        () => this.sampleDeviceConcurrency(),
        1000,
      );
      return;
    }
    clearInterval(this.concurrencyTimer);
    this.concurrencyTimer = undefined;
    this.deviceConcurrency.pauseMeasurements(Date.now());
    if (
      !this.receiving.size &&
      !this.items.some(
        (item) =>
          !item.jobId &&
          item.file &&
          !["completed", "cancelled"].includes(item.state),
      )
    )
      this.deviceConcurrency.reset(Date.now());
  }
  private async sendDeviceChunk(
    chunkId: string,
    expectedOffset: number,
    url: string,
    options: Parameters<UploadChunkTransport>[1],
    progress: Parameters<UploadChunkTransport>[2],
  ) {
    if (!this.canMeasureDeviceUpload())
      this.deviceConcurrency.pauseMeasurements(Date.now());
    this.deviceConcurrency.chunkStarted(chunkId, Date.now(), options.body.size);
    let confirmedBytes: number | undefined;
    try {
      const response = await this.sendChunk(url, options, (sent) => {
        if (options.signal.aborted) return;
        if (!this.canMeasureDeviceUpload())
          this.deviceConcurrency.pauseMeasurements(Date.now());
        this.deviceConcurrency.chunkProgress(chunkId, sent, Date.now());
        progress(sent);
      });
      if (
        response.ok &&
        Number(response.headers.get("Upload-Offset")) === expectedOffset
      )
        confirmedBytes = options.body.size;
      return response;
    } catch (error) {
      if (error instanceof DOMException && error.name === "TimeoutError")
        throw new ApiError(
          "TGDrive stopped confirming this upload. Try Again.",
          408,
          "REQUEST_TIMEOUT",
        );
      if (error instanceof TypeError) throw new ApiError(errorMessage(error));
      throw error;
    } finally {
      this.deviceConcurrency.chunkFinished(chunkId, Date.now(), confirmedBytes);
      this.sampleDeviceConcurrency();
    }
  }
  private async receiveNext(item: Transfer) {
    try {
      await this.receive(item);
    } catch (error) {
      if (
        this.enabled &&
        !this.admissionPaused &&
        !item.controller?.signal.aborted &&
        !item.jobId &&
        !["cancelling", "cancelled"].includes(item.state) &&
        error instanceof ApiError &&
        [0, 408, 429, 500, 502, 503, 504].includes(error.status)
      )
        this.deviceConcurrency.reportPressure(Date.now());
      if (item.jobId) {
        // The server can accept a receipt before its HTTP response is lost.
        // Its polled job remains authoritative over late device-request errors.
        this.refreshJobs();
      } else if (
        this.enabled &&
        !this.admissionPaused &&
        !item.controller?.signal.aborted &&
        !["cancelling", "cancelled"].includes(item.state) &&
        item.uploadId &&
        item.file &&
        error instanceof ApiError &&
        (TRANSIENT_DEVICE_STATUSES.has(error.status) ||
          (error.status === 409 && error.code === "UPLOAD_OFFSET_CHANGED")) &&
        (this.deviceRetryAttempts.get(item.key) || 0) <
          DEVICE_RETRY_DELAYS_MS.length
      ) {
        // Retry only a known receipt: an unacknowledged creation may have
        // succeeded, so automatically repeating its POST could create a second
        // receipt. HEAD/checksums reconcile ambiguous PATCH/completion results.
        const attempt = this.deviceRetryAttempts.get(item.key) || 0;
        this.deviceRetryAttempts.set(item.key, attempt + 1);
        this.stagingRetries.set(
          item.key,
          Date.now() +
            Math.max(DEVICE_RETRY_DELAYS_MS[attempt], error.retryAfter * 1000),
        );
        this.update(item.key, {
          state: "queued",
          stage: "retry_wait",
          phase: "Connection interrupted. Retrying automatically…",
          error: undefined,
        });
      } else if (
        item.state !== "cancelling" &&
        item.state !== "cancelled" &&
        error instanceof ApiError &&
        [429, 507].includes(error.status)
      ) {
        const retryAt = Date.now() + Math.max(5000, error.retryAfter * 1000);
        if (error.status === 429)
          this.admissionRetryAt = Math.max(this.admissionRetryAt, retryAt);
        else this.stagingRetries.set(item.key, retryAt);
        this.update(item.key, {
          state: "queued",
          phase: "Waiting for server space",
          stage: "retry_wait",
          error: undefined,
          ...(item.uploadId ? {} : { startedAt: undefined }),
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
      this.receiving.delete(item.key);
      this.processing.delete(item.key);
      this.startNext();
    }
  }
  private async receive(item: Transfer) {
    const file = item.file!;
    const previouslyAcknowledged = item.current;
    const controller = new AbortController();
    item.controller = controller;
    this.update(item.key, {
      state: "receiving",
      stage: "preparing",
      phase: "Preparing device upload",
      sentCurrent: undefined,
    });
    let snapshot: UploadSnapshot | undefined;
    if (!item.uploadId) {
      snapshot = await api.createUpload(
        item.name,
        item.destination,
        item.size,
        item.folderUpload,
      );
      // Cancellation can remove the row before creation returns. Retain the id
      // on this operation as well so its late receipt can still be cleaned up.
      item.uploadId = snapshot.id;
      this.update(item.key, { uploadId: snapshot.id });
    }
    if (["cancelling", "cancelled"].includes(item.state)) {
      if (item.uploadId) await api.cancelUpload(item.uploadId);
      return;
    }
    if (controller.signal.aborted)
      throw new DOMException("Upload paused", "AbortError");
    const id = item.uploadId!;
    // Creation already returns the authoritative, empty receipt. Rechecking it
    // with HEAD and GET adds two round trips to every small file unnecessarily.
    let offset = snapshot ? snapshot.offset : 0;
    let length = snapshot ? snapshot.total_size : 0;
    if (!snapshot) {
      const head = await fetch("/api/v1/uploads/" + encodeURIComponent(id), {
        method: "HEAD",
        credentials: "same-origin",
        signal: AbortSignal.any([
          controller.signal,
          AbortSignal.timeout(60000),
        ]),
      }).catch((error) => {
        if (error instanceof DOMException && error.name === "TimeoutError")
          throw new ApiError(
            "TGDrive did not confirm the upload position in time. Try Again.",
            408,
            "REQUEST_TIMEOUT",
          );
        if (error instanceof TypeError) throw new ApiError(errorMessage(error));
        throw error;
      });
      if (!head.ok) {
        if (head.status === 401)
          window.dispatchEvent(new Event("tgdrive:auth-required"));
        throw new ApiError(
          head.status === 404 || head.status === 410
            ? "This upload has expired. Cancel it and start a new upload."
            : "Could not check the upload position. Try Again.",
          head.status,
          "",
          Math.max(0, Number(head.headers.get("Retry-After")) || 0),
        );
      }
      const offsetHeader = head.headers.get("Upload-Offset");
      const lengthHeader = head.headers.get("Upload-Length");
      offset =
        offsetHeader === null || offsetHeader === ""
          ? NaN
          : Number(offsetHeader);
      length =
        lengthHeader === null || lengthHeader === ""
          ? NaN
          : Number(lengthHeader);
    }
    if (
      !Number.isSafeInteger(offset) ||
      offset < 0 ||
      offset > file.size ||
      !Number.isSafeInteger(length) ||
      length !== file.size
    )
      throw new Error("The server returned an invalid upload position.");
    snapshot ??= await api.upload(id);
    if (item.jobId) return;
    // A prior PATCH can commit between HEAD and this inspection. Its newer
    // offset and checksum journal belong to one snapshot; never replay the old
    // offset or combine it with a newer journal. A receipt cannot move backward.
    if (
      !Number.isSafeInteger(snapshot.offset) ||
      snapshot.offset < offset ||
      snapshot.offset > file.size ||
      snapshot.total_size !== file.size
    )
      throw new Error("The server returned an invalid upload position.");
    offset = snapshot.offset;
    if (controller.signal.aborted)
      throw new DOMException("Upload paused", "AbortError");
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
    if (offset > previouslyAcknowledged)
      this.deviceRetryAttempts.delete(item.key);
    const chunkSize = snapshot.chunk_size;
    if (
      !Number.isSafeInteger(chunkSize) ||
      chunkSize <= 0 ||
      chunkSize > 32 * 1024 * 1024
    )
      throw new Error("The server returned an invalid chunk size.");
    this.update(item.key, {
      state: "receiving",
      phase: "Uploading from your device",
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
      const response = await this.sendDeviceChunk(
        item.key + ":" + offset,
        end,
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
        let code = "";
        try {
          const failure = await response.json();
          if (typeof failure.detail === "string")
            detail = failure.detail.slice(0, 500);
          if (typeof failure.code === "string") code = failure.code;
        } catch {
          /* A proxy may return a non-JSON error page. */
        }
        notifyDemoFilesReadOnly(code);
        throw new ApiError(
          detail ||
            (response.status === 409
              ? "The upload position changed. Retry to safely resume."
              : response.status === 507
                ? "Temporary Storage is full. Free space or cancel a transfer, then retry."
                : "Sending this file was interrupted. Retry to resume."),
          response.status,
          code,
          Math.max(0, Number(response.headers.get("Retry-After")) || 0),
        );
      }
      const nextOffset = Number(response.headers.get("Upload-Offset"));
      if (nextOffset !== end)
        throw new Error(
          "The server did not confirm this file chunk. Retry to check the upload position.",
        );
      offset = nextOffset;
      this.deviceRetryAttempts.delete(item.key);
      this.update(item.key, { current: offset, sentCurrent: offset });
    }
    if (controller.signal.aborted)
      throw new DOMException("Upload paused", "AbortError");
    this.update(item.key, {
      phase: "Receipt complete. Preparing Telegram upload",
      stage: "confirming",
    });
    // All payload bytes are acknowledged. Let another file send while receipt
    // verification and durable Telegram admission finish in their bounded lane.
    this.receiving.delete(item.key);
    this.startNext();
    await this.completeReceipt(item, controller);
  }
  private startCompletions() {
    if (Date.now() < this.admissionRetryAt) return;
    while (
      this.completing < RECEIPT_COMPLETION_CONCURRENCY &&
      this.completionQueue.length
    )
      this.completionQueue.shift()!();
  }
  private completeReceipt(item: Transfer, controller: AbortController) {
    return new Promise<void>((resolve, reject) => {
      let waiting = true;
      const abort = () => {
        if (!waiting) return;
        waiting = false;
        this.completionQueue = this.completionQueue.filter(
          (entry) => entry !== start,
        );
        reject(new DOMException("Upload paused", "AbortError"));
      };
      const start = () => {
        if (!waiting) return;
        waiting = false;
        controller.signal.removeEventListener("abort", abort);
        this.completing += 1;
        // Do not abandon an admitted completion request on browser cancellation:
        // the server may already own the job, so reconcile its response instead.
        void this.finishReceipt(item)
          .then(resolve, reject)
          .finally(() => {
            this.completing -= 1;
            this.startCompletions();
          });
      };
      if (controller.signal.aborted) {
        abort();
        return;
      }
      controller.signal.addEventListener("abort", abort, { once: true });
      this.completionQueue.push(start);
      this.startCompletions();
    });
  }
  private async finishReceipt(item: Transfer) {
    const result = await api.completeUpload(item.uploadId!).catch((error) => {
      // Publish pressure before the completion lane can admit its next request.
      if (error instanceof ApiError && error.status === 429)
        this.admissionRetryAt = Math.max(
          this.admissionRetryAt,
          Date.now() + Math.max(5000, error.retryAfter * 1000),
        );
      throw error;
    });
    this.deviceRetryAttempts.delete(item.key);
    this.stagingRetries.delete(item.key);
    // Polling or cancellation can finish while the receipt response is in flight.
    // Attach ownership without replacing that newer server/cancellation state.
    if (
      item.jobId ||
      ["cancelling", "cancelled", "completed"].includes(item.state)
    )
      this.update(item.key, { jobId: result.id, file: undefined });
    else
      this.update(item.key, {
        jobId: result.id,
        state: "uploading",
        phase: "Queued for Telegram",
        current: 0,
        total: item.size,
        file: undefined,
      });
    this.refreshJobs();
  }
  private async poll() {
    if (!this.enabled || this.polling) return;
    clearTimeout(this.timer);
    this.polling = true;
    for (const item of this.items
      .filter(
        (row) =>
          row.kind === "remote" &&
          row.remoteRequestId &&
          !row.jobId &&
          !terminal.has(row.state) &&
          !this.remoteSubmitting.has(row.key),
      )
      .slice(0, 8)) {
      this.remoteSubmitting.add(item.key);
      void this.recoverRemote(item.key).finally(() =>
        this.remoteSubmitting.delete(item.key),
      );
    }
    const deletionVersion = this.deletionVersion;
    const historyRevision = this.historyRevision;
    try {
      const payload = await api.jobs();
      let filesChanged = false;
      const previousDeletions = new Map(
        this.deletionJobs.map((job) => [job.id, job.status]),
      );
      const latestDeletions = new Map(
        this.deletionJobs.map((job) => [job.id, job]),
      );
      this.deletionJobs = payload.jobs
        .filter((job) => job.kind === "delete")
        .map((job) => {
          // Do not replace accepted work with a response captured before it started.
          if ((this.deletionUpdates.get(job.id) || 0) > deletionVersion)
            return latestDeletions.get(job.id) || job;
          this.deletionUpdates.delete(job.id);
          return job;
        });
      for (const [id, version] of this.deletionUpdates)
        if (
          version > deletionVersion &&
          !this.deletionJobs.some((job) => job.id === id)
        )
          this.deletionJobs.push(latestDeletions.get(id)!);
      for (const job of this.deletionJobs)
        if (!deletionTerminal.has(job.status))
          this.noticedDeletions.add(job.id);
      this.updateDeletionProgress();
      if (
        this.deletionJobs.some(
          (job) =>
            job.status === "completed" &&
            previousDeletions.get(job.id) !== "completed",
        )
      )
        filesChanged = true;
      const clearedKeys = new Set<string>();
      for (const job of payload.jobs) {
        // Deletions are a single file-operation notice, never upload transfers.
        if (job.kind === "delete" || this.dismissed.has(job.id)) continue;
        if (
          historyRevision !== this.historyRevision &&
          ["completed", "cancelled", "error"].includes(job.status)
        )
          continue;
        let item = this.items.find(
          (item) => item.jobId === job.id || item.uploadId === job.id,
        );
        if (job.history_cleared && !item) continue;
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
        this.deviceRetryAttempts.delete(item.key);
        this.stagingRetries.delete(item.key);
        const wasVisible =
          item.state === "completed" || item.stage === "backup_pending";
        const createdAt = item.createdAt;
        const startedAt = item.startedAt;
        Object.assign(item, fromJob(job, item.kind));
        if (job.history_cleared) clearedKeys.add(item.key);
        if (startedAt)
          item.startedAt = Math.min(startedAt, item.startedAt || startedAt);
        if (createdAt)
          item.createdAt = Math.min(createdAt, item.createdAt || createdAt);
        if (terminal.has(item.state)) item.finishedAt ??= Date.now();
        else item.finishedAt = undefined;
        item.meter = undefined;
        if (
          !wasVisible &&
          (item.state === "completed" || item.stage === "backup_pending")
        )
          filesChanged = true;
      }
      const unfinished = new Set(
        this.items
          .filter((item) => !terminal.has(item.state))
          .map(transferBatchId),
      );
      this.items = this.items.filter(
        (item) =>
          !clearedKeys.has(item.key) || unfinished.has(transferBatchId(item)),
      );
      this.publish();
      if (filesChanged)
        window.dispatchEvent(new Event("tgdrive:files-changed"));
    } catch {
      this.updateDeletionProgress(
        this.deletionJobs.some((job) => !deletionTerminal.has(job.status))
          ? "Could not check deletion progress. Retrying automatically…"
          : "",
      );
      for (const item of this.items)
        if (item.jobId && !terminal.has(item.state))
          item.error = "Progress connection lost. Trying again…";
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
