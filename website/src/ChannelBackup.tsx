import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Download, Pause, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { errorMessage, request } from "./api";
import { ErrorNotice, Spinner } from "./components";
import {
  currentRate,
  formatDuration,
  sampleProgress,
  type ProgressSample,
} from "./progress";
import { formatBytes, formatTimestamp } from "./types";
import "./ChannelBackup.css";

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
  } | null;
}

type ReplicaAction = "sync" | "pause" | "resume" | "retry" | "disable";
const ACTIVE = new Set<ReplicaPhase>(["syncing", "recovering", "waiting"]);
const PHASE_LABEL: Record<ReplicaPhase, string> = {
  disabled: "Not enabled",
  idle: "Ready to sync",
  syncing: "Copying to the backup channel",
  paused: "Paused",
  waiting: "Waiting for Telegram",
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

export default function ChannelBackup({
  recoveryOnly = false,
  onRestartRequired,
}: {
  recoveryOnly?: boolean;
  onRestartRequired?: () => void;
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
  const [messageId, setMessageId] = useState("");
  const [target, setTarget] = useState("");
  const [targetToken, setTargetToken] = useState("");
  const [catalogOnly, setCatalogOnly] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [activationConfirmation, setActivationConfirmation] = useState("");
  const [observedAt, setObservedAt] = useState(Date.now());
  const mounted = useRef(true);
  const generation = useRef(0);
  const progress = useRef<ProgressSample | undefined>(undefined);
  const restartNotified = useRef(false);
  const surface = useRef<HTMLElement>(null);
  const focusResult = useRef<"status" | "candidate" | null>(null);
  const restartCallback = useRef(onRestartRequired);
  restartCallback.current = onRestartRequired;

  const load = useCallback(async (signal?: AbortSignal) => {
    const revision = ++generation.current;
    try {
      const result = await request<ReplicaState>("/api/v1/replica", undefined, {
        signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(20000)])
          : AbortSignal.timeout(20000),
      });
      if (
        !mounted.current ||
        signal?.aborted ||
        revision !== generation.current
      )
        return;
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
    } catch (cause) {
      if (
        mounted.current &&
        !signal?.aborted &&
        revision === generation.current
      )
        setLoadError(errorMessage(cause));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load(controller.signal);
    const visible = () => {
      if (document.visibilityState === "visible") void load(controller.signal);
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
    if (!active && (!state.configured || !state.enabled)) return;
    const controller = new AbortController();
    const timer = setInterval(
      () => {
        if (document.visibilityState === "visible")
          void load(controller.signal);
      },
      active
        ? recoveryOnly || state.phase === "recovering"
          ? 3000
          : 5000
        : 15000,
    );
    return () => {
      clearInterval(timer);
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
    setBusy(label);
    setActionError("");
    try {
      await request(url, payload, { signal: AbortSignal.timeout(120000) });
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
          focusResult.current = label === "inspect" ? "candidate" : "status";
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
    setActionError("");
    try {
      const channelId = parseChannelId(channel);
      if (channelId === state?.source_channel)
        throw new Error("Choose a different channel from the main channel.");
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
      const mainBot = copy ? validateBackupToken(targetToken) : undefined;
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
  const activate = () => {
    if (
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
  const active = ACTIVE.has(phase);
  const blocked = Boolean(busy) || active || phase === "restart_required";
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

  return (
    <section
      className="settings-section channel-backup"
      aria-labelledby="channel-backup-title"
      ref={surface}
    >
      <div className="channel-backup-heading">
        <div>
          <h2 id="channel-backup-title" tabIndex={-1}>
            {recoveryOnly ? "Recover your drive" : "Independent backup channel"}
          </h2>
          <p>
            {recoveryOnly
              ? "Use a surviving channel and your saved recovery key to recover completed files and the catalog."
              : "Keep a second copy of completed files and the catalog in another private channel."}
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh channel backup status"
          disabled={!!busy}
          onClick={() => void load()}
        >
          <RefreshCw size={18} />
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
            onClick={() => void load()}
          >
            Try again
          </button>
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
                  Catalog revision {state.revision} is waiting to be protected.
                  Files not yet confirmed in the backup may still depend on the
                  main channel.
                </p>
              )}
            </div>
          )}
          {showProgress && (
            <div className="channel-backup-progress">
              {state.current_file && (
                <p className="channel-backup-file" title={state.current_file}>
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
                    ? Math.min(state.done_bytes, state.total_bytes)
                    : undefined
                }
              />
              <p className="channel-backup-progress-detail">
                {state.done_parts.toLocaleString()} /{" "}
                {state.total_parts.toLocaleString()} parts confirmed
                {rate ? ` · ${formatBytes(rate)}/s` : ""}
                {rate && remaining > 0
                  ? ` · About ${formatDuration(remaining / rate)} remaining at this rate`
                  : ""}
              </p>
              {phase === "paused" && (
                <p>The saved checkpoint will be used when you resume.</p>
              )}
              {active && (
                <p>You can leave this page. Progress is saved on the server.</p>
              )}
            </div>
          )}
          {state.error && (
            <div className="channel-backup-feedback">
              <ErrorNotice message={state.error.detail} />
              {state.error.action && <p>{state.error.action}</p>}
              {state.error.retry_at && (
                <p>Next retry: {retryTime(state.error.retry_at)}</p>
              )}
            </div>
          )}
          {phase === "restart_required" && (
            <p className="channel-backup-feedback">
              The recovered configuration is saved. Restart TGDrive to open it.
              Keep the surviving channel and your recovery key.
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
              {state.configured && !state.enabled && phase === "disabled" && (
                <button
                  type="button"
                  className="secondary-button"
                  disabled={!!busy}
                  onClick={() => action("resume")}
                >
                  <Play size={16} />
                  {busy === "resume" ? "Enabling…" : "Enable backup"}
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
                    disabled={!!busy || !state.enabled}
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
                  disabled={!!busy}
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
                  disabled={!!busy}
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
                  disabled={!!busy}
                  onClick={() => action("retry")}
                >
                  <RefreshCw size={16} />
                  {busy === "retry" ? "Retrying…" : "Retry"}
                </button>
              )}
              {state.configured && (
                <a className="text-button" href="/api/v1/replica/key" download>
                  <Download size={16} />
                  Save recovery key
                </a>
              )}
            </div>
          )}
          {recoveryOnly &&
            ["paused", "error", "waiting", "recovering"].includes(phase) && (
              <div className="channel-backup-actions">
                {active ? (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={!!busy}
                    onClick={() => action("pause")}
                  >
                    <Pause size={16} />
                    Pause recovery
                  </button>
                ) : (
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={!!busy}
                    onClick={() =>
                      action(phase === "error" ? "retry" : "resume")
                    }
                  >
                    <Play size={16} />
                    {phase === "error" ? "Retry recovery" : "Resume recovery"}
                  </button>
                )}
              </div>
            )}
          {recoveryOnly && candidate && (
            <div className="channel-backup-actions">
              <a className="text-button" href="/api/v1/replica/key" download>
                <Download size={16} />
                Save recovery key
              </a>
            </div>
          )}
          {!recoveryOnly && state.configured && (
            <p className="channel-backup-key-help">
              Save the recovery key outside Telegram. It is needed if this
              installation is lost. A second channel reduces risk; it cannot
              guarantee Telegram access.
            </p>
          )}
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
            Create a private channel from a separate Telegram account. Create a
            backup bot with that account and add it to both channels as an
            administrator with access to messages, posting, deletion and
            pinning. Keep both channels private and let TGDrive manage their
            messages.
          </p>
          <div className="channel-backup-fields">
            <label>
              Backup channel ID
              <input
                inputMode="numeric"
                autoComplete="off"
                value={channel}
                onChange={(event) => setChannel(event.target.value)}
                placeholder="-1001234567890"
                required
                disabled={!!busy}
              />
            </label>
            <label>
              Backup bot token
              <input
                type="password"
                autoComplete="new-password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                spellCheck={false}
                maxLength={512}
                required
                disabled={!!busy}
              />
            </label>
          </div>
          <label className="channel-backup-checkbox">
            <input
              type="checkbox"
              checked={independent}
              onChange={(event) => setIndependent(event.target.checked)}
              disabled={!!busy}
            />
            <span>
              I created the backup channel and bot with a different Telegram
              account from the main channel. TGDrive cannot verify account
              ownership.
            </span>
          </label>
          <div className="channel-backup-actions">
            <button
              type="submit"
              className="primary-button"
              disabled={
                blocked || !channel.trim() || !token.trim() || !independent
              }
            >
              {busy === "configure" ? <Spinner /> : <ShieldCheck size={16} />}
              {busy === "configure" ? "Verifying…" : "Verify and start backup"}
            </button>
            <button
              type="button"
              className="text-button"
              disabled={!!busy}
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
            Existing files are copied first. Protection is confirmed only after
            their file parts and catalog are saved in the backup.
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
                New changes will stop being copied. Keep the existing channels
                and recovery key; disabling does not replace a recovery plan.
              </p>
              <div className="channel-backup-actions">
                <button
                  type="button"
                  className="secondary-button"
                  disabled={!!busy}
                  onClick={() => action("disable")}
                >
                  Confirm disable
                </button>
                <button
                  type="button"
                  className="text-button"
                  disabled={!!busy}
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
        <summary>
          {recoveryOnly
            ? "Recover from a surviving channel"
            : "Recover a channel or catalog"}
        </summary>
        <p>
          {recoveryOnly
            ? "Start with a surviving private channel. Keep it unchanged until recovery is verified."
            : "If the backup channel is lost while the main channel still works, replace it in Backup settings. To rebuild a lost main channel, inspect a surviving recovery copy below."}
        </p>
        {!candidate?.complete && !active && phase !== "restart_required" && (
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
                  onChange={(event) => setSource(event.target.value)}
                  placeholder="-1001234567890"
                  required
                  disabled={!!busy}
                />
              </label>
              <label>
                Bot token with access to the surviving channel
                <input
                  type="password"
                  autoComplete="new-password"
                  value={sourceToken}
                  onChange={(event) => setSourceToken(event.target.value)}
                  spellCheck={false}
                  maxLength={512}
                  required
                  disabled={!!busy}
                />
              </label>
              <label>
                Saved recovery key
                <input
                  type="password"
                  autoComplete="new-password"
                  value={key}
                  onChange={(event) => setKey(event.target.value)}
                  spellCheck={false}
                  maxLength={64}
                  disabled={!!busy}
                  aria-describedby="channel-recovery-key-help"
                />
              </label>
              <label>
                <span>
                  Checkpoint message ID{" "}
                  <span className="channel-backup-optional">(optional)</span>
                </span>
                <input
                  inputMode="numeric"
                  autoComplete="off"
                  value={messageId}
                  onChange={(event) => setMessageId(event.target.value)}
                  disabled={!!busy}
                  aria-describedby="checkpoint-id-help"
                />
              </label>
            </div>
            <p id="channel-recovery-key-help">
              Encrypted copies need the 64-character key value from your saved
              recovery-key JSON. Leave this empty only for a verified plain
              catalog checkpoint.
            </p>
            <p id="checkpoint-id-help">
              Leave the message ID empty to inspect the latest pinned recovery
              copy. Inspecting does not activate or overwrite a drive.
            </p>
            <button
              type="submit"
              className="secondary-button"
              disabled={!!busy || !source.trim() || !sourceToken.trim()}
            >
              {busy === "inspect" ? <Spinner /> : <RefreshCw size={16} />}
              {busy === "inspect" ? "Inspecting…" : "Inspect recovery copy"}
            </button>
          </form>
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
                <dt>Catalog revision</dt>
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
                  {candidate.complete ? "Recovered channel" : "Source channel"}
                </dt>
                <dd>{candidate.channel}</dd>
              </div>
            </dl>
            {!candidate.complete && !active && phase !== "restart_required" && (
              <form
                className="channel-backup-form"
                onSubmit={(event) => recover(event, true)}
              >
                <label className="channel-backup-checkbox">
                  <input
                    type="checkbox"
                    checked={catalogOnly}
                    onChange={(event) => {
                      setCatalogOnly(event.target.checked);
                      setConfirmation("");
                    }}
                    disabled={!!busy}
                  />
                  <span>
                    Restore the database only on this surviving channel
                  </span>
                </label>
                <p>
                  {catalogOnly
                    ? "Files stay in this surviving channel. Add a main bot as an administrator; it must be different from the surviving backup bot. TGDrive checks every stored part before the catalog can be activated."
                    : "Create a new, empty private main channel in Telegram. Add the surviving backup bot and a new main bot as administrators. The main bot must be different from the surviving bot. TGDrive verifies both before copying; the source stays unchanged."}
                </p>
                <div className="channel-backup-fields">
                  {!catalogOnly && (
                    <label>
                      New main channel ID
                      <input
                        inputMode="numeric"
                        autoComplete="off"
                        value={target}
                        onChange={(event) => setTarget(event.target.value)}
                        required
                        disabled={!!busy}
                      />
                    </label>
                  )}{" "}
                  <label>
                    {catalogOnly
                      ? "Main bot token for this channel"
                      : "New main bot token"}
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={targetToken}
                      onChange={(event) => setTargetToken(event.target.value)}
                      spellCheck={false}
                      maxLength={512}
                      required
                      disabled={!!busy}
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
                      onChange={(event) => setConfirmation(event.target.value)}
                      required
                      disabled={!!busy}
                    />
                  </label>
                </div>
                <p>
                  {catalogOnly
                    ? "After activation this channel becomes the main channel. Set up an independent backup again afterwards."
                    : "Copying can take time."}{" "}
                  File changes are paused for recovery. Your existing local
                  database and configuration are preserved until activation.
                </p>
                <button
                  type="submit"
                  className="primary-button"
                  disabled={
                    blocked ||
                    (!catalogOnly && !target.trim()) ||
                    !targetToken.trim() ||
                    (catalogOnly
                      ? String(candidate.channel)
                      : target.trim()) !== confirmation.trim()
                  }
                >
                  {busy === "recover" ? <Spinner /> : <RefreshCw size={16} />}
                  {busy === "recover"
                    ? "Starting recovery…"
                    : catalogOnly
                      ? "Verify catalog on this channel"
                      : "Copy to new main channel"}
                </button>
              </form>
            )}
            {candidate.complete && phase === "ready_to_activate" && (
              <div className="channel-backup-form">
                <p>
                  All required parts and the recovered catalog are confirmed.
                  Activation switches TGDrive to this main channel; a service
                  restart is required.
                </p>
                <label>
                  Type {candidate.channel} to activate this recovered drive
                  <input
                    inputMode="numeric"
                    autoComplete="off"
                    value={activationConfirmation}
                    onChange={(event) =>
                      setActivationConfirmation(event.target.value)
                    }
                    disabled={!!busy}
                  />
                </label>
                <button
                  type="button"
                  className="primary-button"
                  disabled={
                    !!busy ||
                    activationConfirmation.trim() !== String(candidate.channel)
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
      <ErrorNotice message={actionError} />
      {state &&
        (report.length > 0 || state.configured || state.recovery_candidate) && (
          <details className="channel-backup-details">
            <summary>Activity and report</summary>
            {report.length > 0 ? (
              <ol className="channel-backup-report">
                {report.map((entry, index) => (
                  <li key={entry.at + ":" + index}>
                    <time>{formatTimestamp(entry.at)}</time>
                    <span
                      className={entry.level === "error" ? "danger-text" : ""}
                    >
                      {entry.message}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p>No activity reported yet.</p>
            )}
            <a className="text-button" href="/api/v1/replica/report" download>
              <Download size={16} />
              Download report
            </a>
          </details>
        )}
      <a
        className="inline-link channel-backup-guide"
        href="https://github.com/TechShreyash/TGDrive/blob/dev/docs/backup-recovery.md"
        target="_blank"
        rel="noopener noreferrer"
      >
        Backup channel setup and recovery guide
      </a>
    </section>
  );
}
