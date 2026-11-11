import { Alert } from "@mantine/core";
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
import StartNewDrive from "./StartNewDrive";
import ChannelBackup from "../backup/ChannelBackup";
import { resourceBytes } from "../performance/performanceApi";
import { transfers, uploadBackupState } from "../transfers/transfers";
import "./StorageView.css";

export default function StorageView({
    config,
    demoMode = false,
    onTransfers,
    onTrash,
    onNotice,
}: {
    config: { max_file_size: number };
    demoMode?: boolean;
    onTransfers: () => void;
    onTrash: () => void;
    onNotice?: RefreshNotice;
}) {
    const [status, setStatus] = useState<ServerStatus | null>(null);
    const [usage, setUsage] = useState<ManagementStats | null>(null);
    const [error, setError] = useState("");
    const [usageError, setUsageError] = useState("");
    const [loading, setLoading] = useState(true);
    const [backingUp, setBackingUp] = useState(false);
    const [backupError, setBackupError] = useState("");
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
        void poll();
        return () => {
            mounted.current = false;
            ++generation.current;
            controller.abort();
            clearTimeout(timer);
        };
    }, [load]);
    const uploadItems = useSyncExternalStore(
        transfers.subscribe,
        transfers.snapshot,
    );
    const uploadProtection = uploadBackupState(uploadItems);
    const recovery = status?.recovery;
    const unverifiedBackup = status?.backup?.[0]?.status === "unverified";
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
    const independent = recovery?.independent_backup;
    const confirmedIndependent = Boolean(
        independent?.enabled &&
        independent.phase === "protected" &&
        !independent.error &&
        independent.protected_revision != null &&
        independent.protected_revision >=
            (recovery?.metadata_revision ?? Infinity) &&
        independent.last_success_at &&
        Number.isFinite(Date.parse(independent.last_success_at)),
    );
    const priorIndependentSuccess = Boolean(
        independent?.enabled &&
        independent.protected_revision != null &&
        independent.protected_revision > 0 &&
        independent.last_success_at &&
        Number.isFinite(Date.parse(independent.last_success_at)),
    );
    const confirmedDate = independent?.enabled
        ? priorIndependentSuccess
            ? independent.last_success_at
            : null
        : status?.backup?.[0]?.status === "confirmed"
          ? recovery?.last_success_at
          : null;
    const lastBackup = confirmedDate ? new Date(confirmedDate) : null;
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
        if (demoMode) return;
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
    const storageCapacity = usage?.disk_capacity;
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
                    Last Confirmed Telegram Backup:{" "}
                    <strong>
                        {lastBackup && !Number.isNaN(lastBackup.getTime())
                            ? formatTimestamp(confirmedDate)
                            : "Not confirmed yet"}
                    </strong>
                </p>
                <p className="backup-safety-note">
                    Your catalog is saved in MongoDB; files stay in Telegram.
                    Encrypted recovery files are created in the background.
                    Unfinished uploads still need their local files.
                </p>
                {independent?.enabled && !confirmedIndependent && (
                    <p role="status" className="backup-safety-note">
                        {independent.error
                            ? "The independent Telegram backup needs attention."
                            : independent.phase === "syncing"
                              ? "Files are being copied to the independent backup channel."
                              : "The independent Telegram backup has not confirmed the current revision yet."}
                        {independent.error?.detail &&
                            " " + independent.error.detail}
                    </p>
                )}
                {unverifiedBackup &&
                    (independent?.enabled ? (
                        <details className="backup-history-note">
                            <summary>Main-channel backup history</summary>
                            <p>
                                The most recent standalone archive has
                                unverified pin discovery. This is separate from
                                the current independent backup status shown
                                above. Unverified discovery does not establish
                                archive corruption. Keep the archive and your
                                ENCRYPTION_KEY. An intact local archive remains
                                downloadable below.
                            </p>
                        </details>
                    ) : (
                        <Alert
                            color="yellow"
                            variant="light"
                            title="Backup discovery unverified"
                            icon={<AlertCircle size={18} aria-hidden="true" />}
                            role="status"
                        >
                            TGDrive could not verify the latest backup as the
                            channel’s current pin. This does not establish that
                            the archive is corrupt. Keep existing backups and
                            your ENCRYPTION_KEY. If a local copy is available,
                            download it below. Use Back Up Now to publish a
                            fresh recovery point.
                        </Alert>
                    ))}
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
                    Backup and recovery guide
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
                            loading || backingUp || refreshAction.refreshing
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
                        <div
                            className="storage-skeleton-dl"
                            role="status"
                            aria-label="Loading usage"
                        >
                            <span className="sr-only">Loading usage…</span>
                            <span
                                className="skeleton-block"
                                style={{
                                    height: "120px",
                                    borderRadius: "12px",
                                }}
                            />
                        </div>
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
                                    ? "Staging-filesystem free unavailable"
                                    : resourceBytes(usage.free_disk_bytes) +
                                      " reported staging-filesystem free"}
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
                        {storageCapacity?.backing.configured && (
                            <div className="storage-secondary-facts">
                                <span>
                                    Backing filesystem free:{" "}
                                    {storageCapacity.backing.available &&
                                    storageCapacity.backing.free_bytes !== null
                                        ? resourceBytes(
                                              storageCapacity.backing
                                                  .free_bytes,
                                          )
                                        : "Unavailable"}
                                </span>
                                <span>
                                    Free after reserve:{" "}
                                    {storageCapacity.effective.available &&
                                    storageCapacity.effective.headroom_bytes !==
                                        null
                                        ? resourceBytes(
                                              storageCapacity.effective
                                                  .headroom_bytes,
                                          )
                                        : "Unavailable"}
                                </span>
                            </div>
                        )}
                        {storageCapacity?.backing.configured &&
                            !storageCapacity.effective.available && (
                                <p className="settings-help" role="status">
                                    New writes are unavailable because required
                                    storage capacity could not be measured.
                                </p>
                            )}
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
            <StartNewDrive />
        </div>
    );
}
