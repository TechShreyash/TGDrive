import { Button, Paper, Badge } from "@mantine/core";
import {
  useCallback,
  useEffect,
  useState,
  useRef,
  useSyncExternalStore,
  type FormEvent,
} from "react";
import {
  HardDrive,
  ShieldCheck,
  RefreshCw,
  Check,
  AlertCircle,
  X,
  Link,
  Copy,
  ExternalLink,
  Minus,
  Plus,
} from "lucide-react";
import { api, errorMessage, type UploadPreferences } from "./api";
import {
  formatBytes,
  formatDate,
  formatTimestamp,
  type ServerStatus,
  type ManagementStats,
} from "./types";
import { ErrorNotice, Spinner } from "./components";
import { transfers, uploadBackupState } from "./transfers";
import "./OwnerPages.css";
export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(password);
      setPassword("");
      onSuccess();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-page">
      <a href="/" className="brand">
        <span className="brand-symbol">
          <HardDrive size={24} />
        </span>
        <strong>TGDrive</strong>
      </a>
      <section
        id="login-dialog"
        className="login-form"
        aria-labelledby="login-title"
      >
        <ShieldCheck className="login-symbol" size={36} />
        <h1 id="login-title">Your files, your space.</h1>
        <p>Log in to your private drive.</p>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="owner-password">Password</label>
          <div className="password-field">
            <input
              id="owner-password"
              autoFocus
              type={shown ? "text" : "password"}
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              maxLength={512}
            />
            <button
              type="button"
              className="text-button"
              aria-pressed={shown}
              onClick={() => setShown(!shown)}
            >
              {shown ? "Hide" : "Show"}
            </button>
          </div>
          <ErrorNotice message={error} />
          <button className="primary-button full-width" disabled={busy}>
            {busy ? (
              <>
                <Spinner /> Logging in…
              </>
            ) : (
              "Log in"
            )}
          </button>
        </form>
        <p className="login-note">
          Use the owner password configured for this installation.
        </p>
      </section>
      <p className="login-footer">
        Private by default. Share only what you choose.
      </p>
    </main>
  );
}
export { default as SharesView } from "./SharesView";
export function StorageView({
  config,
  onTransfers,
  onTrash,
}: {
  config: { max_file_size: number };
  onTransfers: () => void;
  onTrash: () => void;
}) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [usage, setUsage] = useState<ManagementStats | null>(null);
  const [error, setError] = useState("");
  const [usageError, setUsageError] = useState("");
  const [loading, setLoading] = useState(true);
  const [backingUp, setBackingUp] = useState(false);
  const [backupError, setBackupError] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  const load = useCallback(async (signal?: AbortSignal) => {
    const current = ++generation.current;
    const [health, management] = await Promise.allSettled([
      api.status(signal),
      api.settings(signal),
    ]);
    if (!mounted.current || signal?.aborted || current !== generation.current)
      return;
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
      setUsageError("Usage figures could not be loaded. Check Again to retry.");
    }
    setLoading(false);
  }, []);
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
  const mongo = recovery?.backend === "mongodb";
  const protectedDrive =
    !error &&
    !uploadProtection &&
    recovery?.state === "protected" &&
    !recovery.recovery_required &&
    recovery.protected_revision >= recovery.metadata_revision;
  const attention = Boolean(
    error || recovery?.state === "error" || uploadProtection === "attention",
  );
  const lastBackup = recovery?.last_success_at
    ? new Date(recovery.last_success_at)
    : null;
  const label = loading
    ? "Checking storage…"
    : protectedDrive
      ? mongo
        ? "Saved to MongoDB"
        : "Backed up to Telegram"
      : recovery?.state === "error"
        ? mongo
          ? "Database Needs Attention"
          : "Backup Needs Attention"
        : uploadProtection === "attention"
          ? "Uploads Need Attention"
          : uploadProtection
            ? "Uploads Still in Progress"
            : recovery?.state === "pending"
              ? "Backing up your latest changes"
              : "Backup Status Unavailable";
  const backUp = async () => {
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
  const recoverCommand = mongo
    ? "uv run --frozen python -m tgdrive restore-mongodb --file backup.tgdb"
    : "uv run --frozen python -m tgdrive recover";
  return (
    <div className="storage-page page-body">
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
            <h2 id="drive-backup-heading">
              {mongo ? "Cloud Database" : "Drive Backup"}
            </h2>
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
              {(backingUp || (!protectedDrive && !attention)) && <Spinner />}
              {label}
            </p>
          </div>
          <button
            className="secondary-button"
            disabled={backingUp || loading || !status}
            onClick={() => void backUp()}
          >
            {backingUp ? <Spinner /> : <RefreshCw size={16} />}
            {backingUp ? "Backing up…" : "Back Up Now"}
          </button>
        </div>
        <p className="backup-confirmation-date">
          {mongo ? "Last Telegram Safety Copy:" : "Last Confirmed Backup:"}{" "}
          <strong>
            {lastBackup && !Number.isNaN(lastBackup.getTime())
              ? formatTimestamp(recovery?.last_success_at)
              : mongo
                ? "Scheduled Automatically"
                : "Not Confirmed Yet"}
          </strong>
        </p>
        <p className="backup-safety-note">
          {mongo
            ? "Your catalog is saved in MongoDB; files stay in Telegram. Encrypted database backups run separately. Unfinished uploads still need their local files."
            : "Finish all uploads and wait for confirmation before removing local data. Files still on your device are not backed up."}
        </p>
        <ErrorNotice
          message={
            backupError ||
            error ||
            (typeof recovery?.error === "string" ? recovery.error : "") ||
            (typeof recovery?.backup_error === "string"
              ? recovery.backup_error
              : "")
          }
        />
        <a
          className="inline-link backup-guide-link"
          href="https://github.com/TechShreyash/TGDrive/blob/dev/docs/backup-recovery.md"
          target="_blank"
          rel="noopener noreferrer"
        >
          Backup &amp; recovery guide
        </a>
        {mongo && recovery?.backup_key_configured && (
          <a
            className="text-button"
            href="/api/v1/backup/recovery-key"
            download
          >
            Save Recovery Key
          </a>
        )}
        {!mongo && !protectedDrive && !loading && (
          <p className="settings-help">
            Keep this installation until backup is confirmed. Check the
            connection and the bot’s permission to pin messages if it needs
            attention.
          </p>
        )}
      </section>
      <section
        className="settings-section"
        aria-labelledby="drive-usage-heading"
      >
        <div className="settings-section-title">
          <div>
            <h2 id="drive-usage-heading">Files in This Drive</h2>
            <p>
              Your cataloged files and folders. Telegram does not provide a
              storage quota here.
            </p>
          </div>
          <button
            className="text-button"
            disabled={loading}
            onClick={() => void load()}
          >
            <RefreshCw size={16} /> Check Again
          </button>
        </div>
        <ErrorNotice message={usageError} />
        {usage ? (
          <dl className="storage-catalog">
            <div>
              <dt>Files</dt>
              <dd>
                {usage.files.toLocaleString()}
                <span>{formatBytes(usage.total_bytes)} stored</span>
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
                <button className="storage-count-link" onClick={onTrash}>
                  {usage.trashed_files.toLocaleString()}
                </button>
                <span>
                  {formatBytes(usage.trashed_bytes)} ·{" "}
                  <button className="inline-link" onClick={onTrash}>
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
      <section className="settings-section" aria-labelledby="staging-heading">
        <h2 id="staging-heading">Temporary Storage</h2>
        <p>Reserved space for browser uploads before Telegram confirms them.</p>
        {usage && (
          <div className="staging-usage">
            <div className="staging-meter-label">
              <span>{formatBytes(usage.staging_reserved_bytes)} reserved</span>
              <span>{formatBytes(usage.staging_budget_bytes)} budget</span>
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
            <h2 id="storage-transfers-heading">Current Transfers</h2>
            <p>
              {usage.active_jobs} active · {usage.queued_jobs} queued ·{" "}
              {usage.failed_jobs} need attention
            </p>
          </div>
          <button className="secondary-button" onClick={onTransfers}>
            Open Transfers
          </button>
        </section>
      )}
      <details className="drive-recovery-help">
        <summary>Recover on a New Installation</summary>
        <p>
          {mongo
            ? "Use the same MongoDB connection to reopen this drive on a new installation. A catalog restore is only needed if the database itself is lost."
            : "Configure access to the same Telegram channel, then recover its pinned catalog into a missing database."}
        </p>
        <div className="recovery-command">
          <code>{recoverCommand}</code>
          <button
            type="button"
            className="icon-button"
            aria-label="Copy Recovery Command"
            onClick={() => {
              setCopyError("");
              if (!navigator.clipboard?.writeText) {
                setCopyError("Select and copy the command manually.");
                return;
              }
              void navigator.clipboard
                .writeText(recoverCommand)
                .then(() => setCopied(true))
                .catch(() =>
                  setCopyError("Select and copy the command manually."),
                );
            }}
          >
            {copied ? <Check size={17} /> : <Copy size={17} />}
          </button>
        </div>
        {copied && <p role="status">Recovery command copied.</p>}
        <ErrorNotice message={copyError} />
        <p>
          {mongo
            ? "For emergency restore, download an encrypted .tgdb backup from your private channel, configure an empty MongoDB database and the saved recovery key, then run the command above. Local unfinished file bytes are not included. Existing databases are never overwritten."
            : "Catalog recovery restores completed files and folders, not credentials, unsent originals or unfinished staging. Existing databases are never overwritten."}
        </p>
        <a
          className="inline-link"
          href="https://github.com/TechShreyash/TGDrive/tree/HEAD/docs"
          target="_blank"
          rel="noopener noreferrer"
        >
          Read the Recovery Guide <ExternalLink size={13} />
        </a>
      </details>
    </div>
  );
}

export function UploadSettings() {
  const [saved, setSaved] = useState<UploadPreferences | null>(null);
  const [value, setValue] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const preferences = await api.uploadPreferences(signal);
      if (!signal?.aborted) {
        setSaved(preferences);
        setValue(preferences.uploads_per_bot);
      }
    } catch (cause) {
      if (!signal?.aborted) setError(errorMessage(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const preferences = await api.saveUploadPreferences(value);
      setSaved(preferences);
      setValue(preferences.uploads_per_bot);
      setNotice("Upload settings saved.");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Paper
      component="section"
      className="settings-section preference-section"
      aria-labelledby="upload-settings-heading"
    >
      <div className="preference-description">
        <div className="configuration-section-heading">
          <h2 id="upload-settings-heading">Parallel uploads</h2>
          <Badge color="teal" variant="light">
            Default: 1 per bot
          </Badge>
        </div>
      </div>
      <div className="preference-controls">
        {loading ? (
          <Spinner />
        ) : saved ? (
          <form onSubmit={(event) => void save(event)}>
            <div className="upload-limit-setting">
              <div>
                <label id="upload-limit-label" htmlFor="uploads-per-bot">
                  Simultaneous Uploads per Bot
                </label>
                <p id="upload-limit-help">
                  Start with 1; add bots before increasing.
                </p>
              </div>
              <div className="upload-limit-stepper">
                <Button
                  type="button"
                  className="icon-button"
                  aria-label="Fewer Uploads per Bot"
                  disabled={busy || !Number.isInteger(value) || value <= 1}
                  onClick={() => {
                    setValue(value - 1);
                    setNotice("");
                  }}
                >
                  <Minus size={16} />
                </Button>
                <input
                  id="uploads-per-bot"
                  type="number"
                  min="1"
                  max="4"
                  step="1"
                  required
                  aria-describedby="upload-limit-help"
                  value={value}
                  disabled={busy}
                  onChange={(event) => {
                    setValue(event.target.valueAsNumber);
                    setNotice("");
                  }}
                />
                <Button
                  type="button"
                  className="icon-button"
                  aria-label="More Uploads per Bot"
                  disabled={busy || !Number.isInteger(value) || value >= 4}
                  onClick={() => {
                    setValue(value + 1);
                    setNotice("");
                  }}
                >
                  <Plus size={16} />
                </Button>
              </div>
            </div>
            <p className="settings-help">
              {saved.connected_bots} connected{" "}
              {saved.connected_bots === 1 ? "bot" : "bots"} ·{" "}
              {saved.connected_bots *
                (Number.isInteger(value) ? value : saved.uploads_per_bot)}{" "}
              upload{" "}
              {saved.connected_bots *
                (Number.isInteger(value) ? value : saved.uploads_per_bot) ===
              1
                ? "slot"
                : "slots"}
              .
            </p>
            <div className="upload-settings-actions">
              <Button
                className="primary-button"
                type="submit"
                disabled={
                  busy ||
                  !Number.isInteger(value) ||
                  value < 1 ||
                  value > 4 ||
                  value === saved.uploads_per_bot
                }
              >
                {busy ? "Saving…" : "Save Changes"}
              </Button>
              <Button
                type="button"
                className="text-button"
                variant="subtle"
                disabled={busy}
                onClick={() => {
                  setValue(1);
                  setError("");
                  setNotice(
                    "Default loaded: 1 upload per bot. Save Changes to apply.",
                  );
                }}
              >
                Restore upload defaults
              </Button>
              <Button
                type="button"
                className="text-button"
                variant="subtle"
                disabled={busy || value === saved.uploads_per_bot}
                hidden={value === saved.uploads_per_bot}
                onClick={() => {
                  setValue(saved.uploads_per_bot);
                  setError("");
                  setNotice("");
                }}
              >
                Discard upload changes
              </Button>
              <span role="status" className="settings-help">
                {notice}
              </span>
            </div>
          </form>
        ) : (
          <Button className="secondary-button" onClick={() => void load()}>
            Try Again
          </Button>
        )}
        <ErrorNotice message={error} />
      </div>
    </Paper>
  );
}
