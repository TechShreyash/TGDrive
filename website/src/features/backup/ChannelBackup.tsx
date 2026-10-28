import {
    useCallback,
    useEffect,
    useRef,
    useState,
    type FormEvent,
} from "react";
import { Download, Pause, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { errorMessage, request } from "../../shared/api";
import { useManualRefresh } from "../../shared/useManualRefresh";
import { ErrorNotice, Spinner } from "../../shared/components";
import {
    currentRate,
    formatDuration,
    sampleProgress,
    type ProgressSample,
} from "../transfers/progress";
import { formatBytes, formatTimestamp } from "../../shared/types";
import "./ChannelBackup.css";
import DemoModeNotice from "../../shared/DemoModeNotice";

export type ReplicaPhase =
    | "disabled"
    | "idle"
    | "syncing"
    | "paused"
    | "waiting"
    | "error"
    | "protected"
    | "recovering"
    | "ready_to_activate"
    | "restart_required";
export interface ReplicaState {
    enabled: boolean;
    configured: boolean;
    recovery_only: boolean;
    phase: ReplicaPhase;
    source_channel?: number | null;
    backup_channel?: number | null;
    revision: number;
    protected_revision: number;
    pending_revision?: number;
    last_success_at?: string | null;
    backup_file_available?: boolean;
    file_restore_allowed?: boolean;
    current_file?: string | null;
    total_files: number;
    done_files: number;
    total_parts: number;
    done_parts: number;
    total_bytes: number;
    done_bytes: number;
    error?: {
        code: string;
        detail: string;
        action: string;
        retry_at?: number | string | null;
    } | null;
    report: { at: string; level: string; message: string }[];
    recovery_candidate?: {
        revision: number;
        files: number;
        bytes: number;
        channel: number;
        complete: boolean;
        has_database?: boolean;
        source_is_backup?: boolean;
    } | null;
}

type ReplicaAction = "sync" | "pause" | "resume" | "retry" | "disable";
const ACTIVE = new Set<ReplicaPhase>(["syncing", "recovering", "waiting"]);
const PHASE_LABEL: Record<ReplicaPhase, string> = {
    disabled: "Independent channel not enabled",
    idle: "Ready to sync",
    syncing: "Copying to the backup channel",
    paused: "Paused",
    waiting: "Waiting to retry",
    error: "Needs attention",
    protected: "Backup confirmed",
    recovering: "Preparing the recovered drive",
    ready_to_activate: "Recovered drive ready to activate",
    restart_required: "Restart required",
};

export function parseChannelId(value: string): number {
    const raw = value.trim();
    const id = Number(raw);
    if (!/^-[1-9]\d*$/.test(raw) || !Number.isSafeInteger(id))
        throw new Error(
            "Enter a negative numeric channel ID, such as -1001234567890.",
        );
    return id;
}
export function validateBackupToken(value: string): string {
    const token = value.trim();
    if (!/^[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}$/.test(token))
        throw new Error("Enter the full bot token from BotFather.");
    return token;
}
export function retryTime(value: number | string | null | undefined): string {
    if (!value) return "";
    return formatTimestamp(value);
}

export const MAX_RECOVERY_FILE_BYTES = 64 * 1024 ** 2;

export function validateRecoveryFile(file: Pick<File, "name" | "size">) {
    if (!file.name.toLowerCase().endsWith(".tgdb"))
        throw new Error("Choose your encrypted .tgdb recovery file.");
    if (file.size <= 0 || file.size > MAX_RECOVERY_FILE_BYTES)
        throw new Error("Choose a nonempty recovery file no larger than 64 MiB.");
}

export function recoveryFileKey(value: string): string {
    let key = value.trim();
    if (key.startsWith("{")) {
        try {
            const saved: unknown = JSON.parse(key);
            key =
                saved &&
                typeof saved === "object" &&
                "key" in saved &&
                typeof saved.key === "string"
                    ? saved.key.trim()
                    : "";
        } catch {
            key = "";
        }
    }
    if (!/^[a-fA-F0-9]{64}$/.test(key))
        throw new Error("Paste your saved recovery key or recovery-key JSON.");
    return key;
}

export default function ChannelBackup({
    recoveryOnly = false,
    demoMode = false,
    onRestartRequired,
    onNotice,
}: {
    recoveryOnly?: boolean;
    demoMode?: boolean;
    onRestartRequired?: () => void;
    onNotice?: (message: string, error?: boolean) => void;
}) {
    const [state, setState] = useState<ReplicaState | null>(null);
    const [loadError, setLoadError] = useState("");
    const [actionError, setActionError] = useState("");
    const [busy, setBusy] = useState("");
    const [configureOpen, setConfigureOpen] = useState(false);
    const [channel, setChannel] = useState("");
    const [token, setToken] = useState("");
    const [independent, setIndependent] = useState(false);
    const [disableConfirm, setDisableConfirm] = useState(false);
    const [recoveryOpen, setRecoveryOpen] = useState(recoveryOnly);
    const [source, setSource] = useState("");
    const [sourceToken, setSourceToken] = useState("");
    const [key, setKey] = useState("");
    const [recoveryFile, setRecoveryFile] = useState<File | null>(null);
    const [messageId, setMessageId] = useState("");
    const [target, setTarget] = useState("");
    const [targetToken, setTargetToken] = useState("");
    const [catalogOnly, setCatalogOnly] = useState(false);
    const [confirmation, setConfirmation] = useState("");
    const [activationConfirmation, setActivationConfirmation] = useState("");
    const [observedAt, setObservedAt] = useState(Date.now());
    const mounted = useRef(true);
    const generation = useRef(0);
    const manualPending = useRef(false);
    const importPending = useRef(false);
    const { refreshing, run } = useManualRefresh(onNotice);
    const progress = useRef<ProgressSample | undefined>(undefined);
    const restartNotified = useRef(false);
    const surface = useRef<HTMLElement>(null);
    const actionErrorSummary = useRef<HTMLDivElement>(null);
    const focusResult = useRef<"status" | "candidate" | null>(null);
    const restartCallback = useRef(onRestartRequired);
    restartCallback.current = onRestartRequired;

    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        if (manualPending.current && !manual) return false;
        const revision = ++generation.current;
        try {
            const result = await request<ReplicaState>(
                "/api/v1/replica",
                undefined,
                {
                    signal,
                    timeoutMs: 20000,
                },
            );
            if (
                !mounted.current ||
                signal?.aborted ||
                revision !== generation.current
            )
                return false;
            const now = Date.now();
            progress.current = sampleProgress(
                progress.current,
                result.phase,
                result.done_bytes || 0,
                now,
            );
            setObservedAt(now);
            setState(result);
            setLoadError("");
            return true;
        } catch (cause) {
            if (
                mounted.current &&
                !signal?.aborted &&
                revision === generation.current
            )
                setLoadError(errorMessage(cause));
            if (manual) throw cause;
            return false;
        }
    }, []);
    const refreshStatus = () =>
        run(
            async (signal) => {
                manualPending.current = true;
                try {
                    return await load(signal, true);
                } finally {
                    manualPending.current = false;
                }
            },
            recoveryOnly
                ? "Recovery status refreshed."
                : "Backup status refreshed.",
        );

    useEffect(() => {
        mounted.current = true;
        const controller = new AbortController();
        void load(controller.signal);
        const visible = () => {
            if (document.visibilityState === "visible")
                void load(controller.signal);
        };
        document.addEventListener("visibilitychange", visible);
        return () => {
            mounted.current = false;
            ++generation.current;
            controller.abort();
            document.removeEventListener("visibilitychange", visible);
        };
    }, [load]);
    useEffect(() => {
        if (!state || state.phase === "restart_required") return;
        const active = ACTIVE.has(state.phase);
        if (!active && recoveryOnly && (!state.configured || !state.enabled))
            return;
        const controller = new AbortController();
        const interval = active
            ? recoveryOnly || state.phase === "recovering"
                ? 3000
                : 5000
            : 15000;
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            if (document.visibilityState === "visible")
                await load(controller.signal);
            // Slow responses still update the page before another poll starts.
            if (!controller.signal.aborted) timer = setTimeout(poll, interval);
        };
        timer = setTimeout(poll, interval);
        return () => {
            clearTimeout(timer);
            controller.abort();
        };
    }, [state?.phase, state?.configured, state?.enabled, recoveryOnly, load]);
    useEffect(() => {
        if (state?.phase === "restart_required" && !restartNotified.current) {
            restartNotified.current = true;
            restartCallback.current?.();
        }
    }, [state?.phase]);

    useEffect(() => {
        const candidate = state?.recovery_candidate;
        if (candidate?.has_database && !candidate.complete)
            setCatalogOnly(!candidate.source_is_backup);
    }, [
        state?.recovery_candidate?.channel,
        state?.recovery_candidate?.complete,
        state?.recovery_candidate?.has_database,
        state?.recovery_candidate?.source_is_backup,
    ]);

    useEffect(() => {
        if (actionError) actionErrorSummary.current?.focus();
    }, [actionError]);

    useEffect(() => {
        if (busy || !focusResult.current) return;
        const selector =
            focusResult.current === "candidate"
                ? ".channel-backup-candidate h3"
                : ".channel-backup-heading h2";
        const target = surface.current?.querySelector<HTMLElement>(selector);
        if (!target) return;
        focusResult.current = null;
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "start" });
    }, [state, busy]);

    const mutate = async (
        label: string,
        url: string,
        payload: unknown,
        done?: () => void,
    ) => {
        if (demoMode) return;
        setBusy(label);
        setActionError("");
        try {
            await request(url, payload, {
                timeoutMs: 120000,
            });
            if (mounted.current) {
                if (
                    [
                        "configure",
                        "inspect",
                        "recover",
                        "activate",
                        "pause",
                        "resume",
                        "retry",
                        "disable",
                        "sync",
                    ].includes(label)
                )
                    focusResult.current =
                        label === "inspect" ? "candidate" : "status";
                done?.();
                await load();
            }
        } catch (cause) {
            if (mounted.current) setActionError(errorMessage(cause));
        } finally {
            if (mounted.current) setBusy("");
        }
    };
    const action = (value: ReplicaAction) =>
        void mutate(value, "/api/v1/replica/action", { action: value }, () =>
            setDisableConfirm(false),
        );
    const configure = (event: FormEvent) => {
        event.preventDefault();
        if (demoMode) return;
        setActionError("");
        try {
            const channelId = parseChannelId(channel);
            if (channelId === state?.source_channel)
                throw new Error(
                    "Choose a different channel from the main channel.",
                );
            if (!independent)
                throw new Error(
                    "Confirm that you created the backup with a separate Telegram account.",
                );
            const botToken = validateBackupToken(token);
            void mutate(
                "configure",
                "/api/v1/replica/configure",
                {
                    channel_id: channelId,
                    bot_token: botToken,
                    independent_accounts: true,
                },
                () => {
                    setToken("");
                    setConfigureOpen(false);
                    setIndependent(false);
                },
            );
        } catch (cause) {
            setActionError(errorMessage(cause));
        }
    };
    const recover = (event: FormEvent, copy: boolean) => {
        event.preventDefault();
        if (demoMode) return;
        setActionError("");
        try {
            const sourceChannel =
                copy && state?.recovery_candidate
                    ? state.recovery_candidate.channel
                    : parseChannelId(source);
            const useSavedCopy = copy && !!state?.recovery_candidate;
            const credential =
                !useSavedCopy || sourceToken.trim()
                    ? validateBackupToken(sourceToken)
                    : undefined;
            const recoveryKey = key.trim();
            if (recoveryKey && !/^[0-9a-fA-F]{64}$/.test(recoveryKey))
                throw new Error(
                    "Enter the 64-character hexadecimal key value from the saved recovery-key JSON.",
                );
            let checkpoint: number | undefined;
            if (messageId.trim() && !useSavedCopy) {
                checkpoint = Number(messageId.trim());
                if (
                    !/^[1-9]\d*$/.test(messageId.trim()) ||
                    !Number.isSafeInteger(checkpoint)
                )
                    throw new Error(
                        "The checkpoint message ID must be a positive whole number.",
                    );
            }
            const mainBot =
                copy && (!catalogOnly || targetToken.trim())
                    ? validateBackupToken(targetToken)
                    : undefined;
            const destination = copy
                ? catalogOnly
                    ? sourceChannel
                    : parseChannelId(target)
                : undefined;
            if (copy && !catalogOnly && destination === sourceChannel)
                throw new Error(
                    "Recovery needs a different empty destination channel.",
                );
            if (copy && confirmation.trim() !== String(destination))
                throw new Error(
                    "Type the destination channel ID exactly to confirm recovery.",
                );
            void mutate(
                copy ? "recover" : "inspect",
                "/api/v1/replica/recover",
                {
                    source_channel: sourceChannel,
                    ...(credential ? { bot_token: credential } : {}),
                    ...(recoveryKey ? { key: recoveryKey } : {}),
                    ...(checkpoint ? { message_id: checkpoint } : {}),
                    ...(copy
                        ? {
                              target_channel: destination,
                              target_bot_token: mainBot,
                              catalog_only: catalogOnly,
                              confirm_channel: confirmation.trim(),
                          }
                        : {}),
                },
                () => {
                    setSourceToken("");
                    setKey("");
                    if (copy) setTargetToken("");
                },
            );
        } catch (cause) {
            setActionError(errorMessage(cause));
        }
    };
    const inspectFile = async (event: FormEvent) => {
        event.preventDefault();
        if (fileRestoreBlocked || busy || importPending.current) return;
        setActionError("");
        let headers: Record<string, string>;
        try {
            if (!recoveryFile) throw new Error("Choose your recovery file.");
            validateRecoveryFile(recoveryFile);
            headers = {
                "Content-Type": "application/octet-stream",
                "X-TGDrive-Recovery-Key": recoveryFileKey(key),
            };
            if (source.trim())
                headers["X-TGDrive-Source-Channel"] = String(
                    parseChannelId(source),
                );
            if (sourceToken.trim())
                headers["X-TGDrive-Bot-Token"] = validateBackupToken(sourceToken);
        } catch (cause) {
            setActionError(errorMessage(cause));
            return;
        }
        importPending.current = true;
        setBusy("inspect_file");
        try {
            const result = await request<ReplicaState>(
                "/api/v1/replica/import",
                undefined,
                {
                    method: "POST",
                    body: recoveryFile,
                    headers,
                    timeoutMs: 180000,
                },
            );
            if (!mounted.current) return;
            ++generation.current;
            focusResult.current = "candidate";
            setState(result);
            setLoadError("");
            setConfirmation("");
            setActivationConfirmation("");
            setSourceToken("");
            setKey("");
        } catch (cause) {
            if (mounted.current) setActionError(errorMessage(cause));
        } finally {
            importPending.current = false;
            if (mounted.current) setBusy("");
        }
    };
    const activate = () => {
        if (
            demoMode ||
            activationConfirmation.trim() !==
                String(state?.recovery_candidate?.channel)
        )
            return;
        void mutate(
            "activate",
            "/api/v1/replica/recover",
            {
                activate: true,
                confirm_channel: activationConfirmation.trim(),
            },
            () => {
                setSourceToken("");
                setKey("");
                setActivationConfirmation("");
            },
        );
    };

    const phase = state?.phase || "disabled";
    const fileRestoreBlocked = demoMode || state?.file_restore_allowed === false;
    const active = ACTIVE.has(phase);
    const blocked =
        demoMode || Boolean(busy) || active || phase === "restart_required";
    const candidate = state?.recovery_candidate;
    const pending = !!state && state.revision > state.protected_revision;
    const showProgress =
        !!state &&
        (active ||
            phase === "paused" ||
            phase === "ready_to_activate" ||
            (phase === "error" && state.total_files > 0));
    const rate =
        active && !state?.error && !loadError
            ? currentRate(progress.current, observedAt)
            : undefined;
    const remaining = state
        ? Math.max(0, state.total_bytes - state.done_bytes)
        : 0;
    const report = (state?.report || []).slice(-30).reverse();
    const recoveryKeyAction = demoMode ? (
        <button type="button" className="text-button" disabled>
            <Download size={16} />
            Save recovery key
        </button>
    ) : (
        <a className="text-button" href="/api/v1/replica/key" download>
            <Download size={16} />
            Save recovery key
        </a>
    );

    return (
        <section
            className="settings-section channel-backup"
            aria-labelledby="channel-backup-title"
            aria-busy={!!busy || refreshing}
            ref={surface}
        >
            {demoMode && recoveryOnly && <DemoModeNotice />}
            <div className="channel-backup-heading">
                <div>
                    <h2 id="channel-backup-title" tabIndex={-1}>
                        {recoveryOnly
                            ? "Recover your drive"
                            : "Backups & recovery"}
                    </h2>
                    <p>
                        {recoveryOnly
                            ? "Choose your recovery file and key. Keep the surviving Telegram channel intact while TGDrive prepares recovery."
                            : "Save an encrypted recovery file and its key. Add a second channel to protect your files if a channel is lost."}
                    </p>
                </div>
                <button
                    type="button"
                    className="icon-button"
                    aria-label="Refresh channel backup status"
                    disabled={!!busy || refreshing}
                    onClick={() => void refreshStatus()}
                >
                    {refreshing ? <Spinner /> : <RefreshCw size={18} />}
                </button>
            </div>
            {!state && !loadError && (
                <p className="channel-backup-loading" role="status">
                    <Spinner /> Checking channel backup…
                </p>
            )}
            {loadError && (
                <div className="channel-backup-feedback">
                    <ErrorNotice message={loadError} />
                    <button
                        type="button"
                        className="text-button"
                        disabled={refreshing}
                        onClick={() => void refreshStatus()}
                    >
                        {refreshing && <Spinner />} Try again
                    </button>
                </div>
            )}
            {actionError && (
                <div ref={actionErrorSummary} tabIndex={-1}>
                    <ErrorNotice message={actionError} />
                </div>
            )}
            {state && (
                <>
                    <p className="channel-backup-phase" role="status">
                        {active ? (
                            <Spinner />
                        ) : phase === "protected" ? (
                            <ShieldCheck size={18} aria-hidden="true" />
                        ) : null}
                        <strong>
                            {recoveryOnly && phase === "disabled"
                                ? "Choose a surviving recovery copy"
                                : PHASE_LABEL[phase]}
                        </strong>
                    </p>
                    {!recoveryOnly && state.configured && (
                        <dl className="channel-backup-channels">
                            <div>
                                <dt>Main channel</dt>
                                <dd>{state.source_channel ?? "Unavailable"}</dd>
                            </div>
                            <div>
                                <dt>Backup channel</dt>
                                <dd>{state.backup_channel ?? "Unavailable"}</dd>
                            </div>
                        </dl>
                    )}
                    {!recoveryOnly && state.configured && (
                        <div className="channel-backup-confirmation">
                            <p>
                                Last confirmed restore point:{" "}
                                <strong>
                                    {state.last_success_at
                                        ? formatTimestamp(state.last_success_at)
                                        : "Not confirmed yet"}
                                </strong>
                                {state.protected_revision > 0
                                    ? ` · catalog revision ${state.protected_revision}`
                                    : ""}
                            </p>
                            {pending && (
                                <p>
                                    Catalog revision {state.revision} is waiting
                                    to be protected. Files not yet confirmed in
                                    the backup may still depend on the main
                                    channel.
                                </p>
                            )}
                        </div>
                    )}
                    {showProgress && (
                        <div className="channel-backup-progress">
                            {state.current_file && (
                                <p
                                    className="channel-backup-file"
                                    title={state.current_file}
                                >
                                    {state.current_file}
                                </p>
                            )}
                            <div className="channel-backup-progress-label">
                                <span>
                                    {state.done_files.toLocaleString()} /{" "}
                                    {state.total_files.toLocaleString()} files
                                </span>
                                <span>
                                    {formatBytes(state.done_bytes)} /{" "}
                                    {formatBytes(state.total_bytes)}
                                </span>
                            </div>
                            <progress
                                aria-label="Channel copy progress"
                                max={Math.max(1, state.total_bytes)}
                                value={
                                    state.total_bytes > 0
                                        ? Math.min(
                                              state.done_bytes,
                                              state.total_bytes,
                                          )
                                        : undefined
                                }
                            />
                            <p className="channel-backup-progress-detail">
                                {state.done_parts.toLocaleString()} /{" "}
                                {state.total_parts.toLocaleString()} parts
                                confirmed
                                {rate ? ` · ${formatBytes(rate)}/s` : ""}
                                {rate && remaining > 0
                                    ? ` · About ${formatDuration(remaining / rate)} remaining at this rate`
                                    : ""}
                            </p>
                            {phase === "paused" && (
                                <p>
                                    The saved checkpoint will be used when you
                                    resume.
                                </p>
                            )}
                            {active && (
                                <p>
                                    You can leave this page. Progress is saved
                                    on the server.
                                </p>
                            )}
                        </div>
                    )}
                    {state.error && (
                        <div className="channel-backup-feedback">
                            <ErrorNotice message={state.error.detail} />
                            {state.error.action && <p>{state.error.action}</p>}
                            {Boolean(state.error.retry_at) && (
                                <p>
                                    Next retry:{" "}
                                    {retryTime(state.error.retry_at)}
                                </p>
                            )}
                        </div>
                    )}
                    {phase === "restart_required" && (
                        <p className="channel-backup-feedback">
                            The recovered configuration is saved. Restart
                            TGDrive to open it. Keep the surviving channel and
                            your recovery key.
                        </p>
                    )}
                    {!recoveryOnly && (
                        <div className="channel-backup-actions">
                            {!state.configured && (
                                <button
                                    type="button"
                                    className="primary-button"
                                    disabled={blocked}
                                    onClick={() => {
                                        setConfigureOpen(true);
                                        setActionError("");
                                    }}
                                >
                                    Set up backup channel
                                </button>
                            )}
                            {state.configured &&
                                !state.enabled &&
                                phase === "disabled" && (
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={demoMode || !!busy}
                                        onClick={() => action("resume")}
                                    >
                                        <Play size={16} />
                                        {busy === "resume"
                                            ? "Enabling…"
                                            : "Enable backup"}
                                    </button>
                                )}
                            {state.configured &&
                                state.enabled &&
                                !active &&
                                ![
                                    "ready_to_activate",
                                    "restart_required",
                                    "paused",
                                    "error",
                                ].includes(phase) && (
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={
                                            demoMode || !!busy || !state.enabled
                                        }
                                        onClick={() => action("sync")}
                                    >
                                        <RefreshCw size={16} />
                                        Sync now
                                    </button>
                                )}
                            {active && (
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={demoMode || !!busy}
                                    onClick={() => action("pause")}
                                >
                                    <Pause size={16} />
                                    {busy === "pause" ? "Pausing…" : "Pause"}
                                </button>
                            )}
                            {phase === "paused" && (
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={demoMode || !!busy}
                                    onClick={() => action("resume")}
                                >
                                    <Play size={16} />
                                    {busy === "resume" ? "Resuming…" : "Resume"}
                                </button>
                            )}
                            {phase === "error" && (
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={demoMode || !!busy}
                                    onClick={() => action("retry")}
                                >
                                    <RefreshCw size={16} />
                                    {busy === "retry" ? "Retrying…" : "Retry"}
                                </button>
                            )}
                        </div>
                    )}
                    {recoveryOnly &&
                        ["paused", "error", "waiting", "recovering"].includes(
                            phase,
                        ) && (
                            <div className="channel-backup-actions">
                                {active ? (
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={demoMode || !!busy}
                                        onClick={() => action("pause")}
                                    >
                                        <Pause size={16} />
                                        Pause recovery
                                    </button>
                                ) : (
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={demoMode || !!busy}
                                        onClick={() =>
                                            action(
                                                phase === "error"
                                                    ? "retry"
                                                    : "resume",
                                            )
                                        }
                                    >
                                        <Play size={16} />
                                        {phase === "error"
                                            ? "Retry recovery"
                                            : "Resume recovery"}
                                    </button>
                                )}
                            </div>
                        )}
                    {recoveryOnly && candidate && (
                        <div className="channel-backup-actions">
                            {recoveryKeyAction}
                        </div>
                    )}
                </>
            )}
            {!recoveryOnly && state && (
                <>
                    <div className="channel-backup-actions">
                        {demoMode || !state.backup_file_available ? (
                            <button
                                type="button"
                                className="secondary-button"
                                disabled
                            >
                                <Download size={16} aria-hidden="true" />
                                Download backup
                            </button>
                        ) : (
                            <a
                                className="secondary-button"
                                href="/api/v1/replica/file"
                                download
                            >
                                <Download size={16} aria-hidden="true" />
                                Download backup
                            </a>
                        )}
                        {recoveryKeyAction}
                    </div>
                    <p className="channel-backup-key-help">
                        Keep the recovery file outside the server and save its
                        key separately. Your login password does not unlock it.
                        Download an updated file after a new confirmed backup or
                        channel replacement.
                        {!state.backup_file_available &&
                            " A download is available after the first backup is confirmed."}
                    </p>
                </>
            )}
            {!recoveryOnly && configureOpen && (
                <form className="channel-backup-form" onSubmit={configure}>
                    <h3>
                        {state?.configured
                            ? "Replace backup channel"
                            : "Set up a private backup"}
                    </h3>
                    <p>
                        Create a private channel from a separate Telegram
                        account. Create a backup bot with that account and add
                        it to both channels as an administrator with access to
                        messages, posting, deletion and pinning. Keep both
                        channels private and let TGDrive manage their messages.
                    </p>
                    <div className="channel-backup-fields">
                        <label>
                            Backup channel ID
                            <input
                                inputMode="numeric"
                                autoComplete="off"
                                value={channel}
                                onChange={(event) =>
                                    setChannel(event.target.value)
                                }
                                placeholder="-1001234567890"
                                required
                                disabled={demoMode || !!busy}
                            />
                        </label>
                        <label>
                            Backup bot token
                            <input
                                type="password"
                                autoComplete="new-password"
                                value={token}
                                onChange={(event) =>
                                    setToken(event.target.value)
                                }
                                spellCheck={false}
                                maxLength={512}
                                required
                                disabled={demoMode || !!busy}
                            />
                        </label>
                    </div>
                    <label className="channel-backup-checkbox">
                        <input
                            type="checkbox"
                            checked={independent}
                            onChange={(event) =>
                                setIndependent(event.target.checked)
                            }
                            disabled={demoMode || !!busy}
                        />
                        <span>
                            I created the backup channel and bot with a
                            different Telegram account from the main channel.
                            TGDrive cannot verify account ownership.
                        </span>
                    </label>
                    <div className="channel-backup-actions">
                        <button
                            type="submit"
                            className="primary-button"
                            disabled={
                                blocked ||
                                !channel.trim() ||
                                !token.trim() ||
                                !independent
                            }
                        >
                            {busy === "configure" ? (
                                <Spinner />
                            ) : (
                                <ShieldCheck size={16} />
                            )}
                            {busy === "configure"
                                ? "Verifying…"
                                : "Verify and start backup"}
                        </button>
                        <button
                            type="button"
                            className="text-button"
                            disabled={demoMode || !!busy}
                            onClick={() => {
                                setConfigureOpen(false);
                                setToken("");
                                setIndependent(false);
                            }}
                        >
                            Cancel
                        </button>
                    </div>
                    <p>
                        Existing files are copied first. Protection is confirmed
                        only after their file parts and catalog are saved in the
                        backup.
                    </p>
                </form>
            )}
            {!recoveryOnly && !!state?.configured && (
                <details className="channel-backup-details">
                    <summary>Backup settings</summary>
                    <div className="channel-backup-actions">
                        <button
                            type="button"
                            className="secondary-button"
                            disabled={blocked}
                            onClick={() => {
                                setConfigureOpen(true);
                                setChannel("");
                                setToken("");
                                setIndependent(false);
                            }}
                        >
                            Replace backup channel
                        </button>
                        <button
                            type="button"
                            className="text-button"
                            disabled={blocked}
                            onClick={() => setDisableConfirm(true)}
                        >
                            Disable automatic backup
                        </button>
                    </div>
                    {disableConfirm && (
                        <div className="channel-backup-feedback">
                            <p>
                                New changes will stop being copied. Keep the
                                existing channels and recovery key; disabling
                                does not replace a recovery plan.
                            </p>
                            <div className="channel-backup-actions">
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={demoMode || !!busy}
                                    onClick={() => action("disable")}
                                >
                                    Confirm disable
                                </button>
                                <button
                                    type="button"
                                    className="text-button"
                                    disabled={demoMode || !!busy}
                                    onClick={() => setDisableConfirm(false)}
                                >
                                    Keep enabled
                                </button>
                            </div>
                        </div>
                    )}
                </details>
            )}
            <details
                className="channel-backup-details"
                open={recoveryOpen}
                onToggle={(event) => setRecoveryOpen(event.currentTarget.open)}
            >
                <summary>Restore backup</summary>
                <p>
                    {recoveryOnly
                        ? "Restore the saved drive data using either surviving channel. The source remains intact."
                        : "Restore MongoDB or rebuild a lost main channel from a surviving channel. If only the backup channel was lost, replace it in Backup settings."}
                </p>
                {!candidate?.complete &&
                    !active &&
                    phase !== "restart_required" && (
                        <>
                            <form className="channel-backup-form" onSubmit={inspectFile}>
                                {state?.file_restore_allowed === false && (
                                    <p>
                                        Recovery-file restore is unavailable in demo mode.
                                        Use a private installation for recovery.
                                    </p>
                                )}
                                <div className="channel-backup-fields">
                                    <label>
                                        Recovery file (required)
                                        <input
                                            type="file"
                                            accept=".tgdb"
                                            required
                                            disabled={fileRestoreBlocked || !!busy}
                                            aria-describedby="recovery-file-help"
                                            onChange={(event) => {
                                                setRecoveryFile(event.target.files?.[0] || null);
                                                setActionError("");
                                            }}
                                        />
                                    </label>
                                    <label>
                                        Recovery key (required)
                                        <input
                                            type="password"
                                            autoComplete="off"
                                            value={key}
                                            onChange={(event) => setKey(event.target.value)}
                                            spellCheck={false}
                                            maxLength={8192}
                                            required
                                            disabled={fileRestoreBlocked || !!busy}
                                            aria-describedby="recovery-file-help"
                                        />
                                    </label>
                                </div>
                                <p id="recovery-file-help">
                                    Choose your encrypted .tgdb file, up to 64 MiB. Paste the saved
                                    recovery key or the contents of its JSON file. Your login password
                                    is separate. Inspection does not overwrite the drive.
                                </p>
                                <details>
                                    <summary>Channel access changed?</summary>
                                    <p>
                                        TGDrive tries the channels and credentials in the file.
                                        If a saved token was revoked, grant a replacement bot access
                                        to the surviving channel and enter its token here.
                                    </p>
                                    <div className="channel-backup-fields">
                                        <label>
                                            Surviving channel ID (optional)
                                            <input
                                                inputMode="numeric"
                                                autoComplete="off"
                                                value={source}
                                                onChange={(event) => setSource(event.target.value)}
                                                disabled={fileRestoreBlocked || !!busy}
                                            />
                                        </label>
                                        <label>
                                            Replacement bot token (optional)
                                            <input
                                                type="password"
                                                autoComplete="new-password"
                                                value={sourceToken}
                                                onChange={(event) => setSourceToken(event.target.value)}
                                                spellCheck={false}
                                                maxLength={512}
                                                disabled={fileRestoreBlocked || !!busy}
                                            />
                                        </label>
                                    </div>
                                </details>
                                <button
                                    type="submit"
                                    className="secondary-button"
                                    disabled={fileRestoreBlocked || !!busy || !recoveryFile || !key.trim()}
                                >
                                    {busy === "inspect_file" ? <Spinner /> : <RefreshCw size={16} aria-hidden="true" />}
                                    {busy === "inspect_file" ? "Checking backup…" : "Inspect backup"}
                                </button>
                                {busy === "inspect_file" && (
                                    <p role="status">Keep this page open while the file is sent and its recovery data is checked.</p>
                                )}
                            </form>
                            <details className="channel-backup-details">
                                <summary>Older backups: recover from a channel</summary>
                                <form
                                    className="channel-backup-form"
                                    onSubmit={(event) => recover(event, false)}
                                >
                                    <div className="channel-backup-fields">
                                        <label>
                                            Surviving channel ID
                                            <input
                                                inputMode="numeric"
                                                autoComplete="off"
                                                value={source}
                                                onChange={(event) =>
                                                    setSource(event.target.value)
                                                }
                                                placeholder="-1001234567890"
                                                required
                                                disabled={demoMode || !!busy}
                                            />
                                        </label>
                                        <label>
                                            Bot token with access to the surviving
                                            channel
                                            <input
                                                type="password"
                                                autoComplete="new-password"
                                                value={sourceToken}
                                                onChange={(event) =>
                                                    setSourceToken(event.target.value)
                                                }
                                                spellCheck={false}
                                                maxLength={512}
                                                required
                                                disabled={demoMode || !!busy}
                                            />
                                        </label>
                                        <label>
                                            Saved recovery key
                                            <input
                                                type="password"
                                                autoComplete="new-password"
                                                value={key}
                                                onChange={(event) =>
                                                    setKey(event.target.value)
                                                }
                                                spellCheck={false}
                                                maxLength={64}
                                                disabled={demoMode || !!busy}
                                                aria-describedby="channel-recovery-key-help"
                                            />
                                        </label>
                                        <label>
                                            <span>
                                                Checkpoint message ID{" "}
                                                <span className="channel-backup-optional">
                                                    (optional)
                                                </span>
                                            </span>
                                            <input
                                                inputMode="numeric"
                                                autoComplete="off"
                                                value={messageId}
                                                onChange={(event) =>
                                                    setMessageId(event.target.value)
                                                }
                                                disabled={demoMode || !!busy}
                                                aria-describedby="checkpoint-id-help"
                                            />
                                        </label>
                                    </div>
                                    <p id="channel-recovery-key-help">
                                        Encrypted copies need the 64-character key value
                                        from your saved recovery-key JSON. Leave this
                                        empty only for a verified plain catalog
                                        checkpoint.
                                    </p>
                                    <p id="checkpoint-id-help">
                                        Leave the message ID empty to inspect the latest
                                        pinned recovery copy. Inspecting does not
                                        activate or overwrite a drive.
                                    </p>
                                    <button
                                        type="submit"
                                        className="secondary-button"
                                        disabled={
                                            demoMode ||
                                            !!busy ||
                                            !source.trim() ||
                                            !sourceToken.trim()
                                        }
                                    >
                                        {busy === "inspect" ? (
                                            <Spinner />
                                        ) : (
                                            <RefreshCw size={16} />
                                        )}
                                        {busy === "inspect"
                                            ? "Inspecting…"
                                            : "Inspect recovery copy"}
                                    </button>
                                </form>
                            </details>
                        </>
                    )}
                {candidate && (
                    <div className="channel-backup-candidate">
                        <h3 tabIndex={-1}>
                            {candidate.complete
                                ? "Recovered drive verified"
                                : "Recovery copy found"}
                        </h3>
                        <dl>
                            <div>
                                <dt>Backup revision</dt>
                                <dd>{candidate.revision}</dd>
                            </div>
                            <div>
                                <dt>Completed files</dt>
                                <dd>
                                    {candidate.files.toLocaleString()} ·{" "}
                                    {formatBytes(candidate.bytes)}
                                </dd>
                            </div>
                            <div>
                                <dt>
                                    {candidate.complete
                                        ? "Recovered channel"
                                        : "Source channel"}
                                </dt>
                                <dd>{candidate.channel}</dd>
                            </div>
                        </dl>
                        {candidate.has_database === false && (
                            <p>
                                This older channel recovery copy restores the catalog and
                                supported settings. It does not contain the complete database.
                            </p>
                        )}
                        {!candidate.complete &&
                            !active &&
                            phase !== "restart_required" && (
                                <form
                                    className="channel-backup-form"
                                    onSubmit={(event) => recover(event, true)}
                                >
                                    <label className="channel-backup-checkbox">
                                        <input
                                            type="checkbox"
                                            checked={catalogOnly}
                                            onChange={(event) => {
                                                setCatalogOnly(
                                                    event.target.checked,
                                                );
                                                setConfirmation("");
                                            }}
                                            disabled={demoMode || !!busy}
                                        />
                                        <span>
                                            Restore the database only on this
                                            surviving channel
                                        </span>
                                    </label>
                                    <p>
                                        {catalogOnly
                                            ? "Files stay in this surviving channel, which becomes the main channel. TGDrive verifies every stored part before restoring the database. Enter a replacement main bot token only if the saved access needs to change."
                                            : "Create an empty private main channel. Add the surviving bot and a separate main bot as administrators. TGDrive verifies their access before copying; the surviving backup files remain intact."}
                                    </p>
                                    <div className="channel-backup-fields">
                                        {!catalogOnly && (
                                            <label>
                                                New main channel ID
                                                <input
                                                    inputMode="numeric"
                                                    autoComplete="off"
                                                    value={target}
                                                    onChange={(event) =>
                                                        setTarget(
                                                            event.target.value,
                                                        )
                                                    }
                                                    required
                                                    disabled={
                                                        demoMode || !!busy
                                                    }
                                                />
                                            </label>
                                        )}{" "}
                                        <label>
                                            {catalogOnly
                                                ? "Replacement main bot token (optional)"
                                                : "New main bot token"}
                                            <input
                                                type="password"
                                                autoComplete="new-password"
                                                value={targetToken}
                                                onChange={(event) =>
                                                    setTargetToken(
                                                        event.target.value,
                                                    )
                                                }
                                                spellCheck={false}
                                                maxLength={512}
                                                required={!catalogOnly}
                                                disabled={demoMode || !!busy}
                                            />
                                        </label>
                                        <label>
                                            Type{" "}
                                            {catalogOnly
                                                ? candidate.channel
                                                : "the new main channel ID"}{" "}
                                            to confirm
                                            <input
                                                inputMode="numeric"
                                                autoComplete="off"
                                                value={confirmation}
                                                onChange={(event) =>
                                                    setConfirmation(
                                                        event.target.value,
                                                    )
                                                }
                                                required
                                                disabled={demoMode || !!busy}
                                            />
                                        </label>
                                    </div>
                                    <p>
                                        {catalogOnly
                                            ? "After activation this channel becomes the main channel. Check its independent backup status before relying on new protection."
                                            : "Copying can take time."}{" "}
                                        File changes are paused for recovery.
                                        Your existing local database and
                                        configuration are preserved until
                                        activation.
                                    </p>
                                    <button
                                        type="submit"
                                        className="primary-button"
                                        disabled={
                                            blocked ||
                                            (!catalogOnly && !target.trim()) ||
                                            (!catalogOnly && !targetToken.trim()) ||
                                            (catalogOnly
                                                ? String(candidate.channel)
                                                : target.trim()) !==
                                                confirmation.trim()
                                        }
                                    >
                                        {busy === "recover" ? (
                                            <Spinner />
                                        ) : (
                                            <RefreshCw size={16} />
                                        )}
                                        {busy === "recover"
                                            ? "Starting recovery…"
                                            : catalogOnly
                                              ? "Verify and prepare database restore"
                                              : "Copy to new main channel"}
                                    </button>
                                </form>
                            )}
                        {candidate.complete &&
                            phase === "ready_to_activate" && (
                                <div className="channel-backup-form">
                                    <p>
                                        All required parts and the recovered{" "}
                                        {candidate.has_database === false
                                            ? "catalog"
                                            : "database"}{" "}
                                        data are confirmed. Activation
                                        switches TGDrive to this main channel; a
                                        service restart is required. Keep the
                                        surviving channel intact and save the
                                        updated recovery file after recovery.
                                    </p>
                                    <label>
                                        Type {candidate.channel} to activate
                                        this recovered drive
                                        <input
                                            inputMode="numeric"
                                            autoComplete="off"
                                            value={activationConfirmation}
                                            onChange={(event) =>
                                                setActivationConfirmation(
                                                    event.target.value,
                                                )
                                            }
                                            disabled={demoMode || !!busy}
                                        />
                                    </label>
                                    <button
                                        type="button"
                                        className="primary-button"
                                        disabled={
                                            demoMode ||
                                            !!busy ||
                                            activationConfirmation.trim() !==
                                                String(candidate.channel)
                                        }
                                        onClick={activate}
                                    >
                                        {busy === "activate" ? (
                                            <Spinner />
                                        ) : (
                                            <ShieldCheck size={16} />
                                        )}
                                        {busy === "activate"
                                            ? "Activating…"
                                            : "Activate recovered drive"}
                                    </button>
                                </div>
                            )}
                    </div>
                )}
            </details>
            {state &&
                (report.length > 0 ||
                    state.configured ||
                    state.recovery_candidate) && (
                    <details className="channel-backup-details">
                        <summary>Activity and report</summary>
                        {report.length > 0 ? (
                            <ol className="channel-backup-report">
                                {report.map((entry, index) => (
                                    <li key={entry.at + ":" + index}>
                                        <time>{formatTimestamp(entry.at)}</time>
                                        <span
                                            className={
                                                entry.level === "error"
                                                    ? "danger-text"
                                                    : ""
                                            }
                                        >
                                            {entry.message}
                                        </span>
                                    </li>
                                ))}
                            </ol>
                        ) : (
                            <p>No activity reported yet.</p>
                        )}
                        <a
                            className="text-button"
                            href="/api/v1/replica/report"
                            download
                        >
                            <Download size={16} />
                            Download report
                        </a>
                    </details>
                )}
            <a
                className="inline-link channel-backup-guide"
                href="https://github.com/TechShreyash/TGDrive/blob/main/docs/backup-recovery.md"
                target="_blank"
                rel="noopener noreferrer"
            >
                Backup channel setup and recovery guide
            </a>
        </section>
    );
}
