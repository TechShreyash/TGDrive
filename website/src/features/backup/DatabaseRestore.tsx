import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Upload } from "lucide-react";
import { ApiError, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { formatBytes } from "../../shared/types";
import "./DatabaseRestore.css";

export const MAX_DATABASE_ARCHIVE_BYTES = 1024 ** 3;
export type DatabaseRestoreState =
    | { phase: "idle" | "restoring" }
    | { phase: "restored" }
    | { phase: "restart_required"; message: string };

class DatabaseRestoreError extends ApiError {
    constructor(
        message: string,
        status: number,
        public restartRequired = false,
    ) {
        super(message, status);
    }
}

export function validateDatabaseArchive(file: Pick<File, "name" | "size">) {
    if (!file.name.toLowerCase().endsWith(".tgdb"))
        throw new Error(
            "Choose the encrypted .tgdb database archive from your private channel.",
        );
    if (file.size === 0 || file.size > MAX_DATABASE_ARCHIVE_BYTES)
        throw new Error(
            "Choose a nonempty database archive no larger than 1 GiB.",
        );
}

export function databaseRecoveryKey(value: string) {
    const key = value.trim();
    if (!/^[a-fA-F0-9]{64}$/.test(key))
        throw new Error(
            "Enter the 64-character hexadecimal MONGODB_BACKUP_KEY saved with this archive.",
        );
    return key;
}

/** One attempt only: a lost response can hide an already committed restore. */
export function uploadDatabaseArchive(
    file: File,
    key: string,
    progress: (bytes: number) => void,
): Promise<void> {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        let settled = false;
        const cleanup = () => {
            xhr.upload.onprogress = null;
            xhr.upload.onload = null;
            xhr.onload = null;
            xhr.onerror = null;
            xhr.onabort = null;
        };
        const fail = (error: DatabaseRestoreError) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
        };
        xhr.open("POST", "/api/v1/backup/restore");
        xhr.withCredentials = true;
        // Verification and the database transaction can outlast the normal API
        // timeout. Only the final response confirms whether the restore finished.
        xhr.timeout = 0;
        xhr.setRequestHeader("Accept", "application/json");
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-TGDrive-Recovery-Key", key);
        xhr.setRequestHeader("X-TGDrive-Confirm", "RESTORE");
        xhr.upload.onprogress = (event) => {
            if (!settled && Number.isFinite(event.loaded))
                progress(Math.min(file.size, Math.max(0, event.loaded)));
        };
        xhr.upload.onload = () => {
            if (!settled) progress(file.size);
        };
        xhr.onload = () => {
            if (settled) return;
            let data: Record<string, unknown> = {};
            try {
                const parsed: unknown = JSON.parse(xhr.responseText);
                if (
                    parsed &&
                    typeof parsed === "object" &&
                    !Array.isArray(parsed)
                )
                    data = parsed as Record<string, unknown>;
            } catch {
                // A proxy can return HTML, including after the restore committed.
            }
            if (
                xhr.status >= 200 &&
                xhr.status < 300 &&
                data.restart_required === true
            ) {
                settled = true;
                cleanup();
                resolve();
                return;
            }
            const detail =
                typeof data.detail === "string"
                    ? data.detail
                    : typeof data.error === "string"
                      ? data.error
                      : xhr.status === 401
                        ? "Your session expired. Sign in again before restoring the database."
                        : "TGDrive could not confirm this restore. Check the deployment logs and the database before trying again.";
            fail(
                new DatabaseRestoreError(
                    detail,
                    xhr.status,
                    typeof data.restart_required === "boolean"
                        ? data.restart_required
                        : xhr.status === 0 ||
                              xhr.status >= 500 ||
                              (xhr.status >= 200 && xhr.status < 300),
                ),
            );
        };
        const disconnected = () =>
            fail(
                new DatabaseRestoreError(
                    "The restore response was interrupted. The database may already have been restored. Restart TGDrive and check Storage & Backups before trying again.",
                    0,
                    true,
                ),
            );
        xhr.onerror = disconnected;
        xhr.onabort = disconnected;
        try {
            xhr.send(file);
        } catch {
            fail(
                new DatabaseRestoreError(
                    "The archive could not be sent. Check your connection and choose the file again.",
                    0,
                ),
            );
        }
    });
}

export function DatabaseRestoreResult({ error = "" }: { error?: string }) {
    return (
        <section
            className="database-restore database-restore-result"
            aria-live="polite"
        >
            <h2>
                {error
                    ? "Database restore needs a restart"
                    : "Database restored"}
            </h2>
            <ErrorNotice message={error} />
            <p>
                {error
                    ? "Restart TGDrive, then check which drive opens before trying the restore again."
                    : "Restart TGDrive to open the restored drive. Existing browser sessions have been signed out."}
            </p>
            <ol>
                <li>
                    Use the Restart action for the app in Docker Desktop or
                    Coolify.
                </li>
                <li>
                    Reload this page. A restored drive uses the owner password
                    saved in the archive; credentials explicitly configured in
                    the deployment environment take precedence.
                </li>
                <li>
                    Review files, Connections and shared links before uploading.
                    Older backups can restore previously removed records and
                    revoked shares.
                </li>
            </ol>
            <button
                type="button"
                className="secondary-button"
                onClick={() => window.location.reload()}
            >
                Reload after restart
            </button>
        </section>
    );
}

export default function DatabaseRestore({
    demoMode = false,
    onRestoreStateChange,
}: {
    demoMode?: boolean;
    onRestoreStateChange?: (state: DatabaseRestoreState) => void;
}) {
    const id = useId();
    const [file, setFile] = useState<File | null>(null);
    const [key, setKey] = useState("");
    const [confirmation, setConfirmation] = useState("");
    const [busy, setBusy] = useState(false);
    const [sent, setSent] = useState(0);
    const [error, setError] = useState("");
    const [result, setResult] = useState<{ error?: string } | null>(null);
    const errorSummary = useRef<HTMLDivElement>(null);
    const pending = useRef(false);
    const mounted = useRef(true);
    const stateCallback = useRef(onRestoreStateChange);
    stateCallback.current = onRestoreStateChange;
    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
        };
    }, []);
    useEffect(() => {
        if (error) errorSummary.current?.focus();
    }, [error]);

    const restore = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (demoMode || pending.current) return;
        setError("");
        let recoveryKey: string;
        try {
            if (!file)
                throw new Error("Choose an encrypted .tgdb database archive.");
            validateDatabaseArchive(file);
            recoveryKey = databaseRecoveryKey(key);
            if (confirmation.trim() !== "RESTORE")
                throw new Error(
                    "Type RESTORE to confirm restoring this database archive.",
                );
        } catch (cause) {
            setError(errorMessage(cause));
            return;
        }
        pending.current = true;
        setBusy(true);
        setSent(0);
        stateCallback.current?.({ phase: "restoring" });
        try {
            await uploadDatabaseArchive(file!, recoveryKey, (bytes) => {
                if (mounted.current) setSent(bytes);
            });
            if (mounted.current) {
                setKey("");
                setFile(null);
                setResult({});
            }
            stateCallback.current?.({ phase: "restored" });
        } catch (cause) {
            const message = errorMessage(cause);
            if (
                cause instanceof DatabaseRestoreError &&
                cause.restartRequired
            ) {
                if (mounted.current) {
                    setKey("");
                    setResult({ error: message });
                }
                stateCallback.current?.({ phase: "restart_required", message });
            } else {
                if (mounted.current) setError(message);
                stateCallback.current?.({ phase: "idle" });
            }
        } finally {
            pending.current = false;
            if (mounted.current) setBusy(false);
        }
    };
    if (result) return <DatabaseRestoreResult error={result.error} />;
    return (
        <details className="database-restore database-restore-legacy">
            <summary>Older backups: restore a database archive</summary>
            <p>
                Use this only for an older full MongoDB archive and its matching
                database recovery key. New recovery files use Restore backup
                above. Only an empty, unused drive can receive this archive;
                TGDrive refuses to replace existing files or work.
            </p>
            <p>
                Keep the archive’s matching recovery key. Local unfinished
                upload bytes are not included. Older archives can restore
                removed records and previously revoked shares.
            </p>
            <form
                className="database-restore-form"
                onSubmit={restore}
                aria-busy={busy}
            >
                {error && (
                    <div ref={errorSummary} tabIndex={-1}>
                        <ErrorNotice message={error} />
                    </div>
                )}
                <label htmlFor={`${id}-file`}>
                    Database archive (required)
                    <input
                        id={`${id}-file`}
                        type="file"
                        accept=".tgdb"
                        required
                        disabled={demoMode || busy}
                        aria-describedby={`${id}-file-help`}
                        onChange={(event) => {
                            setFile(event.target.files?.[0] || null);
                            setError("");
                        }}
                    />
                </label>
                <p id={`${id}-file-help`} className="settings-help">
                    Encrypted .tgdb file, up to 1 GiB.
                </p>
                <label htmlFor={`${id}-key`}>
                    Archive recovery key (required)
                    <input
                        id={`${id}-key`}
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        maxLength={128}
                        required
                        value={key}
                        disabled={demoMode || busy}
                        aria-describedby={`${id}-key-help`}
                        onChange={(event) => setKey(event.target.value)}
                    />
                </label>
                <p id={`${id}-key-help`} className="settings-help">
                    Enter the 64-character MONGODB_BACKUP_KEY value from the
                    saved database recovery key. The independent channel
                    recovery key is different.
                </p>
                <label htmlFor={`${id}-confirmation`}>
                    Type RESTORE to confirm (required)
                    <input
                        id={`${id}-confirmation`}
                        autoComplete="off"
                        spellCheck={false}
                        required
                        value={confirmation}
                        disabled={demoMode || busy}
                        onChange={(event) =>
                            setConfirmation(event.target.value)
                        }
                    />
                </label>
                {busy && (
                    <div
                        className="database-restore-progress"
                        role="status"
                        aria-live="polite"
                    >
                        <p>
                            {file && sent < file.size
                                ? `Sending archive: ${formatBytes(sent)} of ${formatBytes(file.size)}`
                                : "Archive sent. Waiting for TGDrive to verify and restore the database…"}
                        </p>
                        <progress
                            aria-label="Database archive upload"
                            value={sent}
                            max={file?.size || 1}
                        />
                        <p>
                            Keep this page open until the result appears. Do not
                            submit another restore while this one is processing.
                        </p>
                    </div>
                )}
                <button
                    type="submit"
                    className="secondary-button"
                    disabled={
                        demoMode ||
                        busy ||
                        !file ||
                        !key ||
                        confirmation.trim() !== "RESTORE"
                    }
                >
                    {busy ? (
                        <Spinner />
                    ) : (
                        <Upload size={16} aria-hidden="true" />
                    )}
                    {busy ? "Restoring database…" : "Restore database archive"}
                </button>
                {demoMode && (
                    <p className="settings-help">
                        Database restore is unavailable in the read-only demo.
                    </p>
                )}
            </form>
        </details>
    );
}
