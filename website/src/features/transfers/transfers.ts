import {
    api,
    ApiError,
    errorMessage,
    notifyDemoFilesReadOnly,
    request,
    sessionExpiryHandler,
} from "../../shared/api";
import type {
    Job,
    UploadSnapshot,
    TransferLocation,
    FolderUploadMetadata,
} from "../../shared/types";
import {
    isArchiveTransfer,
    isServerImport,
    transferTaskId,
} from "./transferTasks";
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
const STORAGE_PREFIX = STORAGE_KEY + ":";
const DISMISSED_KEY = "tgdrive.dismissed-jobs.v1";
const DISMISSED_PREFIX = DISMISSED_KEY + ":";
function isRecoveryRecord(value: unknown, key: string): value is Transfer {
    if (!value || typeof value !== "object") return false;
    const row = value as Transfer;
    return (
        row.key === key &&
        typeof row.name === "string" &&
        typeof row.destination === "string" &&
        Number.isFinite(row.size)
    );
}
function readRecoveryRecords() {
    // The current v2 array is a read-only seed until its rows are updated or cleared.
    let seed: Transfer[] = [];
    let invalid = false;
    try {
        const value: unknown = JSON.parse(
            localStorage.getItem(STORAGE_KEY) || "[]",
        );
        if (!Array.isArray(value)) throw new Error("Invalid recovery array");
        seed = value.filter((row) => row && typeof row.key === "string");
        invalid = seed.length !== value.length;
    } catch {
        invalid = true;
    }
    const rows = new Map<string, Transfer>();
    const add = (key: string, value: Transfer | null) => {
        if (value === null) rows.delete(key);
        else if (!value || typeof value !== "object") invalid = true;
        else if (value.kind !== "delete" && value.initiatedHere === true) {
            if (isRecoveryRecord(value, key)) rows.set(key, value);
            else invalid = true;
        }
    };
    for (const row of seed) add(row.key, row);
    for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key?.startsWith(STORAGE_PREFIX)) continue;
        try {
            add(
                key.slice(STORAGE_PREFIX.length),
                JSON.parse(localStorage.getItem(key)!),
            );
        } catch {
            invalid = true;
        }
    }
    return { rows: [...rows.values()], seed, invalid };
}
export const MAX_BROWSER_QUEUE = 2000;
// Device receipts have their own slots; Telegram work never holds one.
export const BROWSER_RECEIVE_CONCURRENCY = MAX_DEVICE_UPLOAD_CONCURRENCY;
export const BROWSER_RECEIVE_INITIAL_CONCURRENCY =
    INITIAL_DEVICE_UPLOAD_CONCURRENCY;
const MAX_HISTORY_BATCHES = 30;
const MAX_HISTORY_ROWS = 2000;
// Receipt verification/handoff is control work, independent of payload slots.
const RECEIPT_COMPLETION_CONCURRENCY = 2;
const PROGRESS_UPDATE_MS = 250;
const JOB_POLL_INTERVAL_MS = 1500;
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
export function retainTransferBatches(items: Transfer[]): Transfer[] {
    const groups = new Map<string, Transfer[]>();
    for (const item of items) {
        const id = transferTaskId(item);
        const batch = groups.get(id) || [];
        batch.push(item);
        groups.set(id, batch);
    }
    const keep = new Set<string>();
    const finished: { id: string; rows: Transfer[]; time: number }[] = [];
    for (const [id, rows] of groups) {
        if (
            rows.some(
                (item) => !["completed", "cancelled"].includes(item.state),
            )
        )
            keep.add(id);
        else
            finished.push({
                id,
                rows,
                time: Math.max(
                    ...rows.map(
                        (item) =>
                            item.finishedAt ||
                            item.batchCreatedAt ||
                            item.createdAt ||
                            0,
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
    return items.filter((item) => keep.has(transferTaskId(item)));
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
        if (signal?.aborted)
            throw new DOMException("Upload paused", "AbortError");
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
            await file
                .slice(chunk.offset, chunk.offset + chunk.size)
                .arrayBuffer(),
        );
        const hash = [...new Uint8Array(digest)]
            .map((byte) => byte.toString(16).padStart(2, "0"))
            .join("");
        if (hash !== chunk.sha256)
            throw new Error(
                "This file differs from the original upload. Choose the original file or cancel and start again.",
            );
        if (signal?.aborted)
            throw new DOMException("Upload paused", "AbortError");
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
        unfinished.some((item) =>
            ["error", "awaiting_file"].includes(item.state),
        )
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
                : job.status === "cancelling" ||
                    job.status === "cancel_requested"
                  ? "cancelling"
                  : "uploading";
    let phase =
        state === "error"
            ? job.kind === "delete"
                ? "Deletion failed. Retry to finish cleanup"
                : job.phase === "backup_pending"
                  ? "Backup Needs Attention. Retry to secure this file"
                  : job.kind === "torrent"
                    ? "Torrent import failed"
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
                      : job.phase === "source_retry_wait"
                        ? "Source interrupted · retrying automatically in " +
                          Math.max(
                              0,
                              Math.ceil(
                                  (job.retry_at || Date.now() / 1000) -
                                      Date.now() / 1000,
                              ),
                          ) +
                          "s · attempt " +
                          (job.source_attempt || 2) +
                          " of " +
                          (job.source_max_attempts || 4)
                        : job.phase === "retry_wait"
                          ? "Waiting for Telegram; retry scheduled"
                          : ["checking_source", "downloading"].includes(
                                  job.phase,
                              ) && job.download_mode === "waiting_storage"
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
    if (isArchiveTransfer(job)) {
        if (state === "error")
            phase =
                job.kind === "archive"
                    ? "ZIP creation failed"
                    : "Extraction failed";
        else if (state === "completed")
            phase =
                job.kind === "archive"
                    ? "ZIP available in your drive"
                    : "Extracted files available in your drive";
        else if (state === "uploading") {
            if (job.status === "queued") phase = "Queued on server";
            else if (job.phase === "archiving")
                phase = "Creating ZIP on the server";
            else if (job.phase === "extracting")
                phase = "Extracting files on the server";
            else if (job.phase === "downloading")
                phase = "Reading archive from your drive";
            else if (job.phase === "verifying")
                phase = "Checking archive before saving";
            else if (job.phase === "uploading" && job.kind === "extract")
                phase = "Saving extracted files to Telegram";
        }
    }
    if (
        state === "uploading" &&
        (previousKind === "torrent" || job.kind === "torrent")
    ) {
        if (job.phase === "torrent_metadata")
            phase =
                job.status === "queued"
                    ? "Waiting to read torrent"
                    : "Reading torrent metadata…";
        else if (job.phase === "downloading")
            phase = "Downloading from torrent peers";
        else if (job.phase === "torrent_uploading")
            phase = "Importing files to Telegram";
        if (job.torrent_files && job.phase === "torrent_uploading")
            phase +=
                " · " +
                job.torrent_files.completed +
                "/" +
                job.torrent_files.total +
                " files";
    }
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
                " · about " +
                Math.ceil(job.queue.estimated_wait_seconds) +
                "s wait";
    }
    return {
        state,
        phase,
        ...(job.kind === "torrent" ||
        previousKind === "torrent" ||
        isArchiveTransfer(job)
            ? {
                  size: job.total,
                  ...(job.name ? { name: job.name, batchName: job.name } : {}),
                  ...(job.destination ? { destination: job.destination } : {}),
              }
            : {}),
        ...(job.folder_upload
            ? {
                  folderUpload: job.folder_upload,
                  batchId: job.folder_upload.id,
                  batchName: job.folder_upload.name,
                  batchCreatedAt: job.folder_upload.created_at * 1000,
              }
            : {}),
        ...(job.location ? { location: job.location } : {}),
        ...(previousKind === "remote" || previousKind === "torrent"
            ? { kind: previousKind }
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
    private progressTimer: ReturnType<typeof setTimeout> | undefined;
    private stagingTimer: ReturnType<typeof setTimeout> | undefined;
    private polling = false;
    private pollGeneration = 0;
    private pollController: AbortController | undefined;
    private lastPollAt = 0;
    private refreshRequested = false;
    private stagingRetries = new Map<string, number>();
    private deviceRetryAttempts = new Map<string, number>();
    private admissionRetryAt = 0;
    private dismissed = new Set<string>();
    private persistedDismissed = new Set<string>();
    private historyRevision = 0;
    private remoteStarts = new Map<
        string,
        {
            url: string;
            destination: string;
            singleThreaded: boolean;
            requestId: string;
            kind: "remote" | "torrent";
        }
    >();
    private remoteSubmitting = new Set<string>();
    private persisted = new Map<string, string>();
    private restoredRecovery = new Map<string, string>();
    private recoverySeedKeys = new Set<string>();
    private recoveryInvalid = false;
    private historyStorageDirty = false;
    constructor(
        private readonly sendChunk: UploadChunkTransport = sendUploadChunk,
    ) {
        try {
            const saved = readRecoveryRecords();
            this.recoverySeedKeys = new Set(saved.seed.map((row) => row.key));
            this.recoveryInvalid = saved.invalid;
            this.restoredRecovery = new Map(
                saved.rows.map((row) => [row.key, JSON.stringify(row)]),
            );
            if (saved.invalid)
                this.recoveryWarning =
                    "Some transfer recovery information could not be loaded. Valid saved transfers are still available.";
            this.items = saved.rows.map((item) => ({
                ...safeTransferMetadata([item])[0],
                initiatedHere: true,
                batchId: transferTaskId(item),
                batchName:
                    item.folderUpload?.name || item.batchName || item.name,
                batchCreatedAt:
                    (item.folderUpload
                        ? item.folderUpload.created_at * 1000
                        : 0) ||
                    item.batchCreatedAt ||
                    item.createdAt ||
                    Date.now(),
                state:
                    item.jobId || (isServerImport(item) && item.remoteRequestId)
                        ? item.state
                        : terminal.has(item.state)
                          ? item.state
                          : isServerImport(item)
                            ? "error"
                            : "awaiting_file",
                phase:
                    isServerImport(item) &&
                    item.remoteRequestId &&
                    !item.jobId &&
                    !terminal.has(item.state)
                        ? "Checking import status…"
                        : item.jobId
                          ? item.phase
                          : terminal.has(item.state)
                            ? item.phase
                            : isServerImport(item)
                              ? "Import is missing its server job. Start a new import."
                              : "Choose the original file to resume",
            }));
        } catch {
            this.recoveryWarning =
                "Your browser could not load transfer recovery information. Keep this tab open until device transfers finish.";
        }
        this.mergeDismissed();
        this.persistedDismissed = new Set(this.dismissed);
        this.persisted = new Map(
            safeTransferMetadata(this.items).map((row) => [
                row.key,
                JSON.stringify(row),
            ]),
        );
    }
    subscribe = (listener: () => void) => {
        this.listeners.add(listener);
        return () => {
            this.listeners.delete(listener);
        };
    };
    snapshot = () => this.items;
    private reconcileDeviceTransfer(
        item: Transfer,
        records?: Map<string, Transfer>,
        invalidRecords = false,
    ): boolean {
        if (isServerImport(item)) return true;
        let saved: Transfer | undefined;
        let unreadable = false;
        try {
            if (records) saved = records.get(item.key);
            else {
                const recovery = readRecoveryRecords();
                saved = recovery.rows.find((row) => row.key === item.key);
                invalidRecords = recovery.invalid;
            }
            unreadable =
                !saved &&
                invalidRecords &&
                localStorage.getItem(STORAGE_PREFIX + item.key) !== "null";
        } catch {
            this.recoveryWarning =
                "Your browser could not read transfer recovery information. Keep this tab open until device transfers finish.";
            if (!this.persisted.has(item.key)) return true;
            throw new Error(
                "Your browser could not read this transfer's recovery information. Restore browser storage access before resuming or cancelling it.",
            );
        }
        if (!saved) {
            if (!this.persisted.has(item.key)) return true;
            if (unreadable) {
                this.recoveryWarning =
                    "Some transfer recovery information could not be loaded. Valid saved transfers are still available.";
                throw new Error(
                    "This transfer's saved recovery information is unreadable. Keep this tab open and restore browser storage before resuming or cancelling it.",
                );
            }
            item.controller?.abort();
            this.items = this.items.filter((row) => row !== item);
            this.publish(false);
            return false;
        }
        const serialized = JSON.stringify(saved);
        // Restoring a row changes its UI state, not the stored version seen by this tab.
        if (
            serialized !== this.persisted.get(item.key) &&
            serialized !== this.restoredRecovery.get(item.key)
        ) {
            const uploadId = item.uploadId || saved.uploadId;
            const jobId = item.jobId || saved.jobId;
            Object.assign(item, safeTransferMetadata([saved])[0], {
                uploadId,
                jobId,
            });
            if (item.state !== "error") item.error = undefined;
            if (item.jobId || ["completed", "cancelled"].includes(item.state))
                item.file = undefined;
            this.persisted.set(item.key, serialized);
        }
        if (serialized !== this.restoredRecovery.get(item.key))
            this.restoredRecovery.delete(item.key);
        return true;
    }
    private deviceLock<T>(
        key: string,
        work: () => Promise<T>,
        signal?: AbortSignal,
    ): Promise<T> {
        if (!globalThis.navigator?.locks)
            return Promise.reject(
                new Error(
                    "Device uploads require a browser with Web Locks over HTTPS or localhost. Update your browser and reopen TGDrive securely.",
                ),
            );
        return navigator.locks.request(
            "tgdrive.device-upload:" + key,
            { signal },
            work,
        );
    }
    private deviceStorageChanged = (event: StorageEvent) => {
        if (
            event.key === null ||
            event.key === STORAGE_KEY ||
            event.key === DISMISSED_KEY ||
            event.key?.startsWith(DISMISSED_PREFIX)
        )
            this.historyStorageDirty = true;
        if (!event.key?.startsWith(STORAGE_PREFIX)) return;
        let state: TransferState | undefined;
        try {
            const saved = JSON.parse(event.newValue || "null");
            if (
                saved !== null &&
                !isRecoveryRecord(saved, event.key.slice(STORAGE_PREFIX.length))
            ) {
                this.historyStorageDirty = true;
                return;
            }
            if (
                !saved ||
                terminal.has(saved.state) ||
                saved.state === "cancelling"
            )
                this.historyStorageDirty = true;
            if (saved && !["cancelling", "cancelled"].includes(saved.state))
                return;
            state = saved?.state;
        } catch {
            this.historyStorageDirty = true;
            return;
        }
        const item = this.items.find(
            (row) => row.key === event.key!.slice(STORAGE_PREFIX.length),
        );
        if (!item || isServerImport(item)) return;
        // Cancellation is a signal; another tab's older snapshot cannot replace this owner's IDs or offsets.
        item.state = state || "cancelling";
        item.phase = "Cancelling and cleaning up";
        item.controller?.abort();
        if (state === "cancelled") item.file = undefined;
        if (!state) this.items = this.items.filter((row) => row !== item);
        this.publish(false);
    };
    batchProgressSnapshot = () => this.batchProgress.snapshot();
    deletionSnapshot = () => this.deletionProgress;
    private updateDeletionProgress(error = "") {
        this.deletionProgress = {
            jobs: this.deletionJobs,
            completed: this.deletionJobs.filter(
                (job) =>
                    job.status === "completed" &&
                    this.noticedDeletions.has(job.id),
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
            if (job.status === "completed")
                this.noticedDeletions.delete(job.id);
        this.updateDeletionProgress(this.deletionProgress.error);
        this.publish(false);
    };
    refreshJobs = () => {
        if (this.polling) this.refreshRequested = true;
        else void this.poll();
    };
    hasActiveWork = () =>
        this.processing.size > 0 ||
        this.remoteSubmitting.size > 0 ||
        this.items.some(
            (item) =>
                !terminal.has(item.state) &&
                (item.jobId ||
                    (item.state !== "awaiting_file" &&
                        (item.file || item.uploadId || item.remoteRequestId))),
        ) ||
        this.deletionJobs.some((job) => !deletionTerminal.has(job.status));
    private scheduleNextPoll() {
        clearTimeout(this.timer);
        if (!this.enabled) return;
        this.timer = setTimeout(
            () => void this.poll(),
            this.hasActiveWork()
                ? JOB_POLL_INTERVAL_MS
                : typeof document !== "undefined" &&
                    document.visibilityState === "hidden"
                  ? 60000
                  : 30000,
        );
    }
    private pollVisibilityChanged = (event: Event) => {
        if (!this.enabled || this.polling) return;
        if (
            typeof document !== "undefined" &&
            document.visibilityState === "hidden"
        ) {
            if (event.type === "visibilitychange" && !this.hasActiveWork())
                this.scheduleNextPoll();
            return;
        }
        if (Date.now() - this.lastPollAt < JOB_POLL_INTERVAL_MS) {
            if (!this.hasActiveWork()) this.scheduleNextPoll();
        } else void this.poll();
    };
    private scheduleJobsRefresh() {
        if (!this.enabled || this.polling) return;
        clearTimeout(this.timer);
        this.timer = setTimeout(
            () => void this.poll(),
            Math.max(0, this.lastPollAt + JOB_POLL_INTERVAL_MS - Date.now()),
        );
    }
    async retryDeletion(id: string) {
        if (
            !this.deletionJobs.some(
                (job) => job.id === id && job.kind === "delete",
            )
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
        window.dispatchEvent(new Event("tgdrive:files-changed"));
        // A poll started before this retry must be followed by a fresh request.
        this.refreshJobs();
    }
    private mergeDismissed(readStorage = true) {
        if (readStorage) {
            try {
                let saved: unknown;
                try {
                    saved = JSON.parse(
                        localStorage.getItem(DISMISSED_KEY) || "[]",
                    );
                } catch {
                    /* Independent dismissal records remain readable. */
                }
                if (Array.isArray(saved))
                    for (const id of saved)
                        if (typeof id === "string") this.dismissed.add(id);
                const records: [string, number][] = [];
                for (let index = 0; index < localStorage.length; index++) {
                    const key = localStorage.key(index);
                    if (key?.startsWith(DISMISSED_PREFIX))
                        records.push([
                            key,
                            Number(localStorage.getItem(key)) || 0,
                        ]);
                }
                records.sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]));
                for (const [key] of records)
                    this.dismissed.add(key.slice(DISMISSED_PREFIX.length));
                this.dismissed = new Set([...this.dismissed].slice(-1000));
            } catch {
                /* Keep in-memory dismissals when browser storage is unavailable. */
            }
        }
        const unfinished = new Set(
            this.items
                .filter(
                    (item) => !["completed", "cancelled"].includes(item.state),
                )
                .map(transferTaskId),
        );
        this.items = this.items.filter(
            (item) =>
                !item.jobId ||
                !this.dismissed.has(item.jobId) ||
                !["completed", "cancelled"].includes(item.state) ||
                unfinished.has(transferTaskId(item)),
        );
    }
    private publish(persist = true, changed?: Transfer[]) {
        clearTimeout(this.progressTimer);
        this.progressTimer = undefined;
        const now = Date.now();
        if (this.enabled && !this.admissionPaused)
            this.batchProgress.observe(summarizeBatches(this.items, now), now);
        else this.batchProgress.clear();
        if (!persist) {
            this.items = [...this.items];
            for (const listener of this.listeners) listener();
            return;
        }
        // Another tab may have cleared finished rows since the previous poll.
        const previousSize = this.items.length;
        const full = !changed || this.historyStorageDirty;
        this.mergeDismissed(full);
        const retained = retainTransferBatches(this.items);
        const retainedKeys = new Set(retained.map((item) => item.key));
        for (const item of this.items)
            if (item.jobId && !retainedKeys.has(item.key))
                this.dismissed.add(item.jobId);
        this.items = [...retained];
        try {
            if (full || this.items.length !== previousSize) {
                this.persistRecovery();
                this.persistDismissed();
                this.historyStorageDirty = false;
            } else this.persistRecovery(changed!);
            this.recoveryWarning = this.recoveryInvalid
                ? "Some transfer recovery information could not be loaded. Valid saved transfers are still available."
                : "";
        } catch {
            this.recoveryWarning =
                "Your browser could not save transfer recovery information. Keep this tab open until device transfers finish.";
        }
        for (const listener of this.listeners) listener();
    }
    private persistRecovery(changed?: Transfer[]) {
        let seed: Transfer[] = [];
        if (!changed) {
            const saved = readRecoveryRecords();
            seed = saved.seed;
            this.recoverySeedKeys = new Set(seed.map((row) => row.key));
            this.recoveryInvalid = saved.invalid;
        }
        const seedKeys = this.recoverySeedKeys;
        const remove = (key: string) => {
            // A seed tombstone prevents a still-open tab restoring a cleared row.
            if (seedKeys.has(key))
                localStorage.setItem(STORAGE_PREFIX + key, "null");
            else localStorage.removeItem(STORAGE_PREFIX + key);
        };
        for (const row of seed)
            if (!row.initiatedHere || row.kind === "delete") remove(row.key);
        const current = changed ? this.persisted : new Map<string, string>();
        const removed = new Set<string>();
        for (const row of safeTransferMetadata(changed || this.items)) {
            if (!row.initiatedHere || row.kind === "delete") continue;
            const stored = localStorage.getItem(STORAGE_PREFIX + row.key);
            if (
                stored === "null" ||
                (this.persisted.has(row.key) &&
                    stored === null &&
                    !seedKeys.has(row.key))
            ) {
                removed.add(row.key);
                current.delete(row.key);
                continue;
            }
            if (changed && stored !== null) {
                try {
                    if (!isRecoveryRecord(JSON.parse(stored), row.key))
                        this.recoveryInvalid = true;
                } catch {
                    this.recoveryInvalid = true;
                }
            }
            const serialized = JSON.stringify(row);
            if (serialized !== this.persisted.get(row.key))
                localStorage.setItem(STORAGE_PREFIX + row.key, serialized);
            current.set(row.key, serialized);
        }
        if (!changed)
            for (const key of this.persisted.keys())
                if (!current.has(key) && !removed.has(key)) remove(key);
        this.items = this.items.filter((row) => !removed.has(row.key));
        this.persisted = current;
        if (changed) return;
        const saved = readRecoveryRecords().rows;
        const retained = new Set(
            retainTransferBatches(saved).map((row) => row.key),
        );
        for (const row of saved) if (!retained.has(row.key)) remove(row.key);
        if (
            seed.length &&
            seed.every(
                (row) =>
                    localStorage.getItem(STORAGE_PREFIX + row.key) !== null,
            )
        ) {
            localStorage.removeItem(STORAGE_KEY);
            for (const row of seed)
                if (localStorage.getItem(STORAGE_PREFIX + row.key) === "null")
                    localStorage.removeItem(STORAGE_PREFIX + row.key);
            this.recoverySeedKeys.clear();
        }
    }
    private persistDismissed() {
        for (const id of this.dismissed)
            if (
                !this.persistedDismissed.has(id) &&
                localStorage.getItem(DISMISSED_PREFIX + id) === null
            )
                localStorage.setItem(DISMISSED_PREFIX + id, String(Date.now()));
        const records: [string, number][] = [];
        for (let index = 0; index < localStorage.length; index++) {
            const key = localStorage.key(index);
            if (key?.startsWith(DISMISSED_PREFIX))
                records.push([key, Number(localStorage.getItem(key)) || 0]);
        }
        records.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
        for (const [key] of records.slice(1000)) localStorage.removeItem(key);
        if (records.length >= 1000) localStorage.removeItem(DISMISSED_KEY);
        this.persistedDismissed = new Set(this.dismissed);
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
            const progressOnly = Object.keys(values).every(
                (key) => key === "sentCurrent",
            );
            if (progressOnly && (item.sentCurrent ?? 0) < item.total)
                this.progressTimer ??= setTimeout(
                    () => this.publish(false),
                    PROGRESS_UPDATE_MS,
                );
            else this.publish(!progressOnly, [item]);
        }
    }
    setEnabled(enabled: boolean) {
        if (this.enabled === enabled) return;
        this.pollGeneration++;
        this.enabled = enabled;
        if (!enabled) {
            this.pollController?.abort();
            this.pollController = undefined;
            this.polling = false;
            this.refreshRequested = false;
            window.removeEventListener("storage", this.deviceStorageChanged);
            window.removeEventListener("focus", this.pollVisibilityChanged);
            if (typeof document !== "undefined")
                document.removeEventListener(
                    "visibilitychange",
                    this.pollVisibilityChanged,
                );
            this.batchProgress.clear();
            clearTimeout(this.timer);
            clearTimeout(this.progressTimer);
            this.progressTimer = undefined;
            clearTimeout(this.stagingTimer);
            clearInterval(this.concurrencyTimer);
            this.concurrencyTimer = undefined;
            this.deviceConcurrency.pauseMeasurements(Date.now());
            for (const item of this.items) item.controller?.abort();
            return;
        }
        window.addEventListener("storage", this.deviceStorageChanged);
        window.addEventListener("focus", this.pollVisibilityChanged);
        if (typeof document !== "undefined")
            document.addEventListener(
                "visibilitychange",
                this.pollVisibilityChanged,
            );
        this.historyStorageDirty = true;
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
                .filter(
                    (item) => !["completed", "cancelled"].includes(item.state),
                )
                .map(transferTaskId),
        );
        const retainedRows = this.items.filter((item) =>
            unfinished.has(transferTaskId(item)),
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
            folderUpload: FolderUploadMetadata;
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
        const createdAt = folder ? folder.created_at * 1000 : Date.now();
        for (const { file, destination, relativePath } of accepted) {
            const key = crypto.randomUUID();
            this.items.push({
                key,
                initiatedHere: true,
                createdAt: Date.now(),
                batchId: folder?.id || key,
                batchName: folder?.name || file.name,
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
        kind: "remote" | "torrent" = "remote",
    ): string {
        url = url.trim();
        let parsed: URL;
        try {
            parsed = new URL(url);
        } catch {
            throw new Error(
                kind === "torrent"
                    ? "Enter a magnet link or a public HTTP or HTTPS .torrent URL."
                    : "Enter a direct HTTP or HTTPS file link.",
            );
        }
        const magnet = kind === "torrent" && parsed.protocol === "magnet:";
        if (
            (!["http:", "https:"].includes(parsed.protocol) && !magnet) ||
            parsed.username ||
            parsed.password ||
            (magnet &&
                !parsed.searchParams
                    .getAll("xt")
                    .some((value) =>
                        /^urn:btih:(?:[a-f0-9]{40}|[a-z2-7]{32})$/i.test(value),
                    ))
        )
            throw new Error(
                kind === "torrent"
                    ? "Enter a v1 or hybrid magnet link, or a public HTTP or HTTPS .torrent URL."
                    : "Enter a direct HTTP or HTTPS file link.",
            );
        if (!this.canQueue(1))
            throw new Error(
                "Finish or cancel an existing transfer before adding more.",
            );
        const key = crypto.randomUUID();
        let name = magnet
            ? parsed.searchParams.get("dn") || "Torrent import"
            : parsed.pathname.split("/").at(-1) ||
              (kind === "torrent" ? "Torrent import" : "URL import");
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
            kind,
            remoteRequestId: requestId,
            state: "uploading",
            stage: kind === "torrent" ? "torrent_metadata" : "checking_source",
            phase:
                kind === "torrent"
                    ? "Starting torrent import…"
                    : "Starting URL import…",
        });
        this.remoteStarts.set(key, {
            url,
            destination,
            singleThreaded,
            requestId,
            kind,
        });
        this.publish();
        void this.submitRemote(key);
        return key;
    }
    startTorrent(url: string, destination: string, requestId: string): string {
        return this.startRemote(url, destination, false, requestId, "torrent");
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
            Object.assign(item, fromJob(result.data, item.kind));
        else if (!["cancelled", "cancelling"].includes(item.state)) {
            item.state = "uploading";
            item.phase =
                item.kind === "torrent"
                    ? "Reading torrent metadata…"
                    : "Checking source…";
            item.stage =
                item.kind === "torrent"
                    ? "torrent_metadata"
                    : "checking_source";
            item.error = undefined;
        }
        this.publish();
    }
    private async recoverRemote(key: string, originalError?: unknown) {
        const item = this.items.find((row) => row.key === key);
        if (!item?.remoteRequestId) return;
        const generation = this.pollGeneration;
        try {
            const result = await api.remoteRequest(item.remoteRequestId);
            if (generation !== this.pollGeneration) return;
            if (!result.id && result.data.status === "queued")
                throw new Error(
                    "The server is still accepting this import. Retry to check it.",
                );
            this.acceptRemote(key, result);
        } catch (error) {
            if (generation !== this.pollGeneration) return;
            if (["cancelled", "cancelling"].includes(item.state)) return;
            this.update(key, {
                state: "error",
                stage:
                    item.kind === "torrent"
                        ? "torrent_metadata"
                        : "checking_source",
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
                pending.kind === "torrent"
                    ? await api.torrent(
                          pending.url,
                          pending.destination,
                          pending.requestId,
                      )
                    : await api.remote(
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
        if (isServerImport(item))
            throw new Error(
                "Imports resume on the server. Retry the transfer instead.",
            );
        if (
            !this.reconcileDeviceTransfer(item) ||
            item.jobId ||
            ["completed", "cancelled", "cancelling"].includes(item.state)
        ) {
            this.publish(false);
            return;
        }
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
        let recovery: ReturnType<typeof readRecoveryRecords>;
        try {
            recovery = readRecoveryRecords();
        } catch {
            throw new Error(
                "Your browser could not read transfer recovery information. Restore browser storage access before resuming this folder.",
            );
        }
        const saved = new Map(recovery.rows.map((row) => [row.key, row]));
        const batch = this.items
            .filter((item) => transferTaskId(item) === batchId)
            .filter((item) =>
                this.reconcileDeviceTransfer(item, saved, recovery.invalid),
            );
        const pending = batch.filter(
            (item) =>
                !item.jobId &&
                (item.state === "awaiting_file" || item.state === "error"),
        );
        const folderBatch = batch.some((item) => Boolean(item.folderUpload));
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
            selected.delete(key);
        }
        let skipped = 0;
        for (const item of batch) {
            if (pending.includes(item)) continue;
            const key = itemKey(item);
            const candidates = selected.get(key);
            if (!candidates?.length) continue;
            const remaining = candidates.filter(
                (file) =>
                    file.name.normalize("NFC") !== item.name.normalize("NFC") ||
                    file.size !== item.size,
            );
            skipped += candidates.length - remaining.length;
            selected.set(key, remaining);
        }
        if (matched) {
            this.publish();
            void this.startNext();
        }
        return {
            matched,
            skipped,
            remaining: pending.length - matched,
            unmatched: files.length - matched - skipped,
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
        } else if (isServerImport(item)) {
            this.update(key, {
                state: "uploading",
                error: undefined,
                phase: "Checking import status…",
            });
            if (this.remoteStarts.has(key)) await this.submitRemote(key);
            else if (item.remoteRequestId) await this.recoverRemote(key);
            else
                throw new Error(
                    "This import has no server job. Start a new import.",
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
        if (
            !item ||
            !this.reconcileDeviceTransfer(item) ||
            !canCancelTransfer(item)
        )
            return;
        this.stagingRetries.delete(item.key);
        this.deviceRetryAttempts.delete(item.key);
        this.update(key, {
            state: "cancelling",
            phase: "Cancelling and cleaning up",
            error: undefined,
        });
        item.controller?.abort();
        const cancel = async () => {
            try {
                if (!this.reconcileDeviceTransfer(item)) return;
                if (item.jobId) {
                    const result = await api.cancelJob(item.jobId);
                    if (result.data)
                        this.update(key, fromJob(result.data, item.kind));
                } else if (isServerImport(item) && item.remoteRequestId) {
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
                if (
                    error instanceof ApiError &&
                    error.code === "BACKUP_PENDING"
                ) {
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
        };
        if (isServerImport(item) || !globalThis.navigator?.locks)
            await cancel();
        else await this.deviceLock(item.key, cancel);
    }
    clearFinished() {
        for (const item of this.items)
            if (item.jobId && ["completed", "cancelled"].includes(item.state))
                this.dismissed.add(item.jobId);
        const unfinished = new Set(
            this.items
                .filter(
                    (item) => !["completed", "cancelled"].includes(item.state),
                )
                .map(transferTaskId),
        );
        this.items = this.items.filter((item) =>
            unfinished.has(transferTaskId(item)),
        );
        this.publish();
    }
    async clearHistory() {
        const unfinished = new Set(
            this.items
                .filter(
                    (item) =>
                        (!item.jobId &&
                            !isServerImport(item) &&
                            item.state === "error") ||
                        !["completed", "cancelled", "error"].includes(
                            item.state,
                        ),
                )
                .map(transferTaskId),
        );
        const removed = this.items.filter(
            (item) =>
                item.kind !== "delete" && !unfinished.has(transferTaskId(item)),
        );
        const keys = new Set(removed.map((item) => item.key));
        const excludeFolders = [
            ...new Set(
                this.items
                    .filter((item) => unfinished.has(transferTaskId(item)))
                    .flatMap((item) =>
                        item.folderUpload ? [item.folderUpload.id] : [],
                    ),
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
                (item) =>
                    transferTaskId(item) === batchId && canCancelTransfer(item),
            ),
        );
    }
    async retryBatch(batchId: string) {
        for (const item of this.items.filter(
            (item) =>
                transferTaskId(item) === batchId && item.state === "error",
        ))
            await this.retry(item.key);
    }
    async cancelAll(keys?: string[]) {
        const selected = keys ? new Set(keys) : null;
        await this.cancelItems(
            this.items.filter(
                (item) =>
                    canCancelTransfer(item) &&
                    (!selected || selected.has(item.key)),
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
                const batches = [...new Set(eligible.map(transferTaskId))];
                const previous = batches.indexOf(this.lastReceivingBatch || "");
                const nextBatch = batches[(previous + 1) % batches.length];
                const item = eligible.find(
                    (candidate) => transferTaskId(candidate) === nextBatch,
                );
                if (!item) break;
                this.lastReceivingBatch = transferTaskId(item);
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
            if (this.hasActiveWork()) this.scheduleJobsRefresh();
        }
    }
    private canMeasureDeviceUpload() {
        return (
            this.enabled &&
            !this.admissionPaused &&
            (typeof document === "undefined" ||
                document.visibilityState !== "hidden")
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
        this.deviceConcurrency.chunkStarted(
            chunkId,
            Date.now(),
            options.body.size,
        );
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
            if (error instanceof TypeError)
                throw new ApiError(errorMessage(error));
            throw error;
        } finally {
            this.deviceConcurrency.chunkFinished(
                chunkId,
                Date.now(),
                confirmedBytes,
            );
            this.sampleDeviceConcurrency();
        }
    }
    private async receiveNext(item: Transfer) {
        const controller = new AbortController();
        item.controller = controller;
        try {
            await this.deviceLock(
                item.key,
                async () => {
                    if (!this.reconcileDeviceTransfer(item)) return;
                    if (
                        item.jobId ||
                        ["completed", "cancelled", "cancelling"].includes(
                            item.state,
                        )
                    ) {
                        this.publish(false);
                        return;
                    }
                    if (!this.enabled || controller.signal.aborted)
                        throw new DOMException("Upload paused", "AbortError");
                    await this.receive(item, controller);
                },
                controller.signal,
            );
        } catch (error) {
            try {
                this.reconcileDeviceTransfer(item);
            } catch (recoveryError) {
                this.update(item.key, {
                    state: "error",
                    error: errorMessage(recoveryError),
                });
                return;
            }
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
                    (error.status === 409 &&
                        error.code === "UPLOAD_OFFSET_CHANGED")) &&
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
                        Math.max(
                            DEVICE_RETRY_DELAYS_MS[attempt],
                            error.retryAfter * 1000,
                        ),
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
                const retryAt =
                    Date.now() + Math.max(5000, error.retryAfter * 1000);
                if (error.status === 429)
                    this.admissionRetryAt = Math.max(
                        this.admissionRetryAt,
                        retryAt,
                    );
                else this.stagingRetries.set(item.key, retryAt);
                this.update(item.key, {
                    state: "queued",
                    phase: "Waiting for server space",
                    stage: "retry_wait",
                    error: undefined,
                    ...(item.uploadId ? {} : { startedAt: undefined }),
                });
            } else if (
                item.state !== "cancelling" &&
                item.state !== "cancelled"
            )
                this.update(item.key, {
                    state:
                        error instanceof DOMException &&
                        error.name === "AbortError"
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
    private async receive(item: Transfer, controller: AbortController) {
        const file = item.file!;
        const previouslyAcknowledged = item.current;
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
            // The cancelling tab owns cleanup after this operation releases its lock.
            if (
                item.uploadId &&
                (item.state === "cancelled" || !this.items.includes(item))
            )
                await api.cancelUpload(item.uploadId);
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
            const expireSession = sessionExpiryHandler();
            const head = await fetch(
                "/api/v1/uploads/" + encodeURIComponent(id),
                {
                    method: "HEAD",
                    credentials: "same-origin",
                    signal: AbortSignal.any([
                        controller.signal,
                        AbortSignal.timeout(60000),
                    ]),
                },
            ).catch((error) => {
                if (
                    error instanceof DOMException &&
                    error.name === "TimeoutError"
                )
                    throw new ApiError(
                        "TGDrive did not confirm the upload position in time. Try Again.",
                        408,
                        "REQUEST_TIMEOUT",
                    );
                if (error instanceof TypeError)
                    throw new ApiError(errorMessage(error));
                throw error;
            });
            if (!head.ok) {
                expireSession(head.status);
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
            const expireSession = sessionExpiryHandler();
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
                    if (controller.signal.aborted || item.state !== "receiving")
                        return;
                    this.update(item.key, {
                        sentCurrent: Math.max(
                            item.sentCurrent ?? offset,
                            offset + sent,
                        ),
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
                expireSession(response.status);
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
                    Math.max(
                        0,
                        Number(response.headers.get("Retry-After")) || 0,
                    ),
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
        const result = await api
            .completeUpload(item.uploadId!)
            .catch((error) => {
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
        this.scheduleJobsRefresh();
    }
    private async poll() {
        if (!this.enabled || this.polling) return;
        clearTimeout(this.timer);
        this.polling = true;
        const generation = this.pollGeneration;
        const controller = new AbortController();
        this.pollController = controller;
        this.lastPollAt = Date.now();
        for (const item of this.items
            .filter(
                (row) =>
                    isServerImport(row) &&
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
            const payload = await api.jobs(controller.signal);
            if (generation !== this.pollGeneration) return;
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
                    if (
                        (this.deletionUpdates.get(job.id) || 0) >
                        deletionVersion
                    )
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
                        deletionTerminal.has(job.status) &&
                        previousDeletions.get(job.id) !== job.status,
                )
            )
                filesChanged = true;
            this.mergeDismissed(this.historyStorageDirty);
            const changed: Transfer[] = [];
            const clearedKeys = new Set<string>();
            for (const job of payload.jobs) {
                // Deletions are a single file-operation notice, never upload transfers.
                if (job.kind === "delete" || this.dismissed.has(job.id))
                    continue;
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
                        batchCreatedAt: job.created_at
                            ? job.created_at * 1000
                            : Date.now(),
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
                    item.state === "completed" ||
                    item.stage === "backup_pending";
                const createdAt = item.createdAt;
                const startedAt = item.startedAt;
                Object.assign(item, fromJob(job, item.kind));
                if (job.history_cleared) clearedKeys.add(item.key);
                if (startedAt)
                    item.startedAt = Math.min(
                        startedAt,
                        item.startedAt || startedAt,
                    );
                if (createdAt)
                    item.createdAt = Math.min(
                        createdAt,
                        item.createdAt || createdAt,
                    );
                if (terminal.has(item.state)) item.finishedAt ??= Date.now();
                else item.finishedAt = undefined;
                item.meter = undefined;
                changed.push(item);
                if (
                    !wasVisible &&
                    (item.state === "completed" ||
                        item.stage === "backup_pending")
                )
                    filesChanged = true;
            }
            const unfinished = new Set(
                this.items
                    .filter((item) => !terminal.has(item.state))
                    .map(transferTaskId),
            );
            const previousSize = this.items.length;
            this.items = this.items.filter(
                (item) =>
                    !clearedKeys.has(item.key) ||
                    unfinished.has(transferTaskId(item)),
            );
            this.publish(
                true,
                this.items.length === previousSize ? changed : undefined,
            );
            if (filesChanged)
                window.dispatchEvent(new Event("tgdrive:files-changed"));
        } catch {
            if (generation !== this.pollGeneration) return;
            this.updateDeletionProgress(
                this.deletionJobs.some(
                    (job) => !deletionTerminal.has(job.status),
                )
                    ? "Could not check deletion progress. Retrying automatically…"
                    : "",
            );
            for (const item of this.items)
                if (item.jobId && !terminal.has(item.state))
                    item.error = "Progress connection lost. Trying again…";
            this.publish(false);
        } finally {
            if (generation !== this.pollGeneration) return;
            this.polling = false;
            this.pollController = undefined;
            if (this.refreshRequested && this.enabled) {
                this.refreshRequested = false;
                void this.poll();
                return;
            }
            this.refreshRequested = false;
            if (this.enabled) void this.startNext();
            this.scheduleNextPoll();
        }
    }
}
export const transfers = new TransferManager();
export function isTransferActive(item: Transfer) {
    return !terminal.has(item.state);
}
