import {
    useCallback,
    useEffect,
    useRef,
    useState,
    useSyncExternalStore,
} from "react";
import {
    AlertCircle,
    ExternalLink,
    RefreshCw,
    ShieldCheck,
} from "lucide-react";
import { api, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import DemoModeNotice from "../../shared/DemoModeNotice";
import {
    formatBytes,
    formatTimestamp,
    type ManagementStats,
    type ServerStatus,
} from "../../shared/types";
import {
    useManualRefresh,
    type RefreshNotice,
} from "../../shared/useManualRefresh";
import ChannelBackup from "../backup/ChannelBackup";
import DatabaseRestore, {
    type DatabaseRestoreState,
} from "../backup/DatabaseRestore";
import { transfers, uploadBackupState } from "../transfers/transfers";
import "./StorageView.css";

export default function StorageView({
    config,
    demoMode = false,
    onTransfers,
    onTrash,
    onNotice,
    onRestoreStateChange,
}: {
    config: { max_file_size: number };
    demoMode?: boolean;
    onTransfers: () => void;
    onTrash: () => void;
    onNotice?: RefreshNotice;
    onRestoreStateChange?: (state: DatabaseRestoreState) => void;
}) {
    const [status, setStatus] = useState<ServerStatus | null>(null);
    const [usage, setUsage] = useState<ManagementStats | null>(null);
    const [error, setError] = useState("");
    const [usageError, setUsageError] = useState("");
    const [loading, setLoading] = useState(true);
    const [backingUp, setBackingUp] = useState(false);
    const [backupError, setBackupError] = useState("");
    const [restoringDatabase, setRestoringDatabase] = useState(false);
    const generation = useRef(0);
    const mounted = useRef(true);
    const manualPending = useRef(false);
    const refreshAction = useManualRefresh(onNotice);
    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        if (manualPending.current && !manual) return false;
        const current = ++generation.current;
        const [health, management] = await Promise.allSettled([
            api.status(signal),
            api.settings(signal),
        ]);
        if (
            !mounted.current ||
            signal?.aborted ||
            current !== generation.current
        )
            return false;
        if (health.status === "fulfilled") {
            setStatus(health.value);
            setError("");
        } else {
            setStatus(null);
            setError(errorMessage(health.reason));
        }
        if (management.status === "fulfilled") {
            setUsage(management.value.stats);
            setUsageError("");
        } else {
            setUsage(null);
            setUsageError(
                "Usage figures could not be loaded. Check Again to retry.",
            );
        }
        setLoading(false);
        if (manual) {
            if (health.status === "rejected") throw health.reason;
            if (management.status === "rejected") throw management.reason;
        }
        return true;
    }, []);
    const refreshStorage = () =>
        refreshAction.run(async (signal) => {
            manualPending.current = true;
            try {
                return await load(signal, true);
            } finally {
                manualPending.current = false;
            }
        }, "Storage refreshed.");
    useEffect(() => {
        mounted.current = true;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            await load(controller.signal);
            if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
        };
        if (!restoringDatabase) void poll();
        return () => {
            mounted.current = false;
            ++generation.current;
            controller.abort();
            clearTimeout(timer);
        };
    }, [load, restoringDatabase]);
    const uploadItems = useSyncExternalStore(
        transfers.subscribe,
        transfers.snapshot,
    );
    const uploadProtection = uploadBackupState(uploadItems);
    const recovery = status?.recovery;
    const protectedDrive =
        !error &&
        !uploadProtection &&
        recovery?.state === "protected" &&
        !recovery.recovery_required &&
        recovery.protected_revision >= recovery.metadata_revision;
    const attention = Boolean(
        error ||
        recovery?.state === "error" ||
        uploadProtection === "attention",
    );
    const lastBackup = recovery?.last_success_at
        ? new Date(recovery.last_success_at)
        : null;
    const label = loading
        ? "Checking storage…"
        : protectedDrive
          ? "Saved to MongoDB"
          : recovery?.state === "error"
            ? "Database Needs Attention"
            : uploadProtection === "attention"
              ? "Uploads Need Attention"
              : uploadProtection
                ? "Uploads Still in Progress"
                : recovery?.state === "pending"
                  ? "Backing up your latest changes"
                  : "Backup Status Unavailable";
    const backUp = async () => {
        if (demoMode || restoringDatabase) return;
        setBackingUp(true);
        setBackupError("");
        try {
            await api.backup();
        } catch (cause) {
            if (mounted.current) setBackupError(errorMessage(cause));
        } finally {
            if (mounted.current) {
                await load();
                setBackingUp(false);
            }
        }
    };
    const databaseRestoreState = useCallback(
        (state: DatabaseRestoreState) => {
            if (mounted.current) setRestoringDatabase(state.phase !== "idle");
            onRestoreStateChange?.(state);
        },
        [onRestoreStateChange],
    );
    return (
        <div className="storage-page page-body">
            {demoMode && <DemoModeNotice />}
            <section
                className="settings-section storage-protection"
                aria-labelledby="drive-backup-heading"
            >
                <div className="storage-protection-heading">
                    <div
                        className={
                            "storage-protection-icon " +
                            (protectedDrive
                                ? "is-protected"
                                : attention
                                  ? "is-attention"
                                  : "")
                        }
                    >
                        {protectedDrive ? (
                            <ShieldCheck size={25} />
                        ) : attention ? (
                            <AlertCircle size={25} />
                        ) : (
                            <RefreshCw size={25} />
                        )}
                    </div>
                    <div>
                        <h2 id="drive-backup-heading">Cloud Database</h2>
                        <p
                            className={
                                "drive-backup-state " +
                                (protectedDrive
                                    ? "connection-ready"
                                    : attention
                                      ? "danger-text"
                                      : "")
                            }
                            role="status"
                        >
                            {(backingUp || (!protectedDrive && !attention)) && (
                                <Spinner />
                            )}
                            {label}
                        </p>
                    </div>
                    <button
                        className="secondary-button"
                        disabled={
                            demoMode ||
                            restoringDatabase ||
                            backingUp ||
                            loading ||
                            refreshAction.refreshing ||
                            !status
                        }
                        onClick={() => void backUp()}
                    >
                        {backingUp ? <Spinner /> : <RefreshCw size={16} />}
                        {backingUp ? "Backing up…" : "Back Up Now"}
                    </button>
                </div>
                <p className="backup-confirmation-date">
                    Last Telegram Safety Copy:{" "}
                    <strong>
                        {lastBackup && !Number.isNaN(lastBackup.getTime())
                            ? formatTimestamp(recovery?.last_success_at)
                            : "Scheduled Automatically"}
                    </strong>
                </p>
                <p className="backup-safety-note">
                    Your catalog is saved in MongoDB; files stay in Telegram.
                    Encrypted recovery files are created in the background.
                    Unfinished uploads still need their local files.
                </p>
                <ErrorNotice
                    message={
                        backupError ||
                        error ||
                        (typeof recovery?.error === "string"
                            ? recovery.error
                            : "") ||
                        (typeof recovery?.backup_error === "string"
                            ? recovery.backup_error
                            : "")
                    }
                />
                <a
                    className="inline-link backup-guide-link"
                    href="https://github.com/TechShreyash/TGDrive/blob/main/docs/backup-recovery.md"
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    Backup &amp; recovery guide
                </a>
            </section>
            <ChannelBackup demoMode={demoMode} onNotice={onNotice} />
            <section
                className="settings-section"
                aria-labelledby="drive-usage-heading"
            >
                <div className="settings-section-title">
                    <div>
                        <h2 id="drive-usage-heading">Files in This Drive</h2>
                        <p>
                            Your cataloged files and folders. Telegram does not
                            provide a storage quota here.
                        </p>
                    </div>
                    <button
                        className="text-button"
                        aria-label="Check Again"
                        disabled={
                            loading ||
                            backingUp ||
                            restoringDatabase ||
                            refreshAction.refreshing
                        }
                        onClick={() => void refreshStorage()}
                        aria-busy={refreshAction.refreshing}
                    >
                        {refreshAction.refreshing ? (
                            <Spinner />
                        ) : (
                            <RefreshCw size={16} />
                        )}
                        {refreshAction.refreshing
                            ? "Refreshing…"
                            : "Check Again"}
                    </button>
                </div>
                <ErrorNotice message={usageError} />
                {usage ? (
                    <dl className="storage-catalog">
                        <div>
                            <dt>Files</dt>
                            <dd>
                                {usage.files.toLocaleString()}
                                <span>
                                    {formatBytes(usage.total_bytes)} stored
                                </span>
                            </dd>
                        </div>
                        <div>
                            <dt>Folders</dt>
                            <dd>
                                {usage.folders.toLocaleString()}
                                <span>Across your drive</span>
                            </dd>
                        </div>
                        <div>
                            <dt>In Trash</dt>
                            <dd>
                                <button
                                    className="storage-count-link"
                                    onClick={onTrash}
                                >
                                    {usage.trashed_files.toLocaleString()}
                                </button>
                                <span>
                                    {formatBytes(usage.trashed_bytes)} ·{" "}
                                    <button
                                        className="inline-link"
                                        onClick={onTrash}
                                    >
                                        Open Trash
                                    </button>
                                </span>
                            </dd>
                        </div>
                    </dl>
                ) : (
                    !usageError && (
                        <p role="status">
                            <Spinner /> Loading usage…
                        </p>
                    )
                )}
            </section>
            <section
                className="settings-section"
                aria-labelledby="staging-heading"
            >
                <h2 id="staging-heading">Temporary Storage</h2>
                <p>
                    Reserved space for browser uploads before Telegram confirms
                    them.
                </p>
                {usage && (
                    <div className="staging-usage">
                        <div className="staging-meter-label">
                            <span>
                                {formatBytes(usage.staging_reserved_bytes)}{" "}
                                reserved
                            </span>
                            <span>
                                {formatBytes(usage.staging_budget_bytes)} budget
                            </span>
                        </div>
                        <progress
                            className="staging-meter"
                            value={usage.staging_reserved_bytes}
                            max={Math.max(1, usage.staging_budget_bytes)}
                            aria-label="Reserved Upload Staging"
                        />
                        <div className="storage-secondary-facts">
                            <span>
                                {usage.free_disk_bytes == null
                                    ? "Free Disk Unavailable"
                                    : formatBytes(usage.free_disk_bytes) +
                                      " free on the staging disk"}
                            </span>
                            <span>
                                File limit:{" "}
                                {formatBytes(
                                    typeof status?.max_file_size === "number"
                                        ? status.max_file_size
                                        : config.max_file_size,
                                )}
                            </span>
                        </div>
                    </div>
                )}
            </section>
            {usage && (
                <section
                    className="settings-section storage-jobs"
                    aria-labelledby="storage-transfers-heading"
                >
                    <div>
                        <h2 id="storage-transfers-heading">
                            Current Transfers
                        </h2>
                        <p>
                            {usage.active_jobs} active · {usage.queued_jobs}{" "}
                            queued · {usage.failed_jobs} need attention
                        </p>
                    </div>
                    <button className="secondary-button" onClick={onTransfers}>
                        Open Transfers
                    </button>
                </section>
            )}
            <details className="drive-recovery-help">
                <summary>
                    Reopen an existing drive or restore an older archive
                </summary>
                <p>
                    Use the same MongoDB connection to reopen this drive on a
                    new installation. If MongoDB was lost, use Restore backup
                    above with your saved recovery file and key.
                </p>
                <DatabaseRestore
                    demoMode={demoMode}
                    onRestoreStateChange={databaseRestoreState}
                />
                <a
                    className="inline-link"
                    href="https://github.com/TechShreyash/TGDrive/blob/main/docs/backup-recovery.md"
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    Read the Recovery Guide <ExternalLink size={13} />
                </a>
            </details>
        </div>
    );
}
