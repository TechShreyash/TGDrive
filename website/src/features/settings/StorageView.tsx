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
import { formatTimestamp, type ServerStatus } from "../../shared/types";
import {
    useManualRefresh,
    type RefreshNotice,
} from "../../shared/useManualRefresh";
import ChannelBackup from "../backup/ChannelBackup";
import { transfers, uploadBackupState } from "../transfers/transfers";
import "./StorageView.css";

export default function StorageView({
    demoMode = false,
    onNotice,
}: {
    demoMode?: boolean;
    onNotice?: RefreshNotice;
}) {
    const [status, setStatus] = useState<ServerStatus | null>(null);
    const [error, setError] = useState("");
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
        try {
            const value = await api.status(signal);
            if (
                !mounted.current ||
                signal?.aborted ||
                current !== generation.current
            )
                return false;
            setStatus(value);
            setError("");
            return true;
        } catch (cause) {
            if (
                !mounted.current ||
                signal?.aborted ||
                current !== generation.current
            )
                return false;
            setStatus(null);
            setError(errorMessage(cause));
            if (manual) throw cause;
            return false;
        } finally {
            if (
                mounted.current &&
                !signal?.aborted &&
                current === generation.current
            )
                setLoading(false);
        }
    }, []);
    const refreshStatus = () =>
        refreshAction.run(async (signal) => {
            manualPending.current = true;
            try {
                return await load(signal, true);
            } finally {
                manualPending.current = false;
            }
        }, "Backup status refreshed.");
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
                            loading || backingUp || refreshAction.refreshing
                        }
                        aria-busy={refreshAction.refreshing}
                        onClick={() => void refreshStatus()}
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
        </div>
    );
}
