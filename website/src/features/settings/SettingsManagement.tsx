import {
    useCallback,
    useEffect,
    useRef,
    useState,
    useSyncExternalStore,
    type FormEvent,
} from "react";
import {
    AlertCircle,
    Bot,
    Check,
    ChevronDown,
    KeyRound,
    Pencil,
    Plus,
    RefreshCw,
    Undo2,
    UserRound,
    X,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../../shared/api";
import { useManualRefresh } from "../../shared/useManualRefresh";
import { ErrorNotice, Spinner } from "../../shared/components";
import type { ManagedClient, ManagementSettings } from "../../shared/types";
import {
    clientConnectionLabel,
    clientDisplayName,
    createConnectionDraft,
    isCredentialRepair,
    MAX_STORAGE_CLIENTS,
    prepareConnectionUpdate,
    type ClientDraft,
    type ConnectionDraft,
} from "./settingsManagement";
import { transfers, uploadBackupState } from "../transfers/transfers";
import PremiumConnection from "./PremiumConnection";
import DemoModeNotice from "../../shared/DemoModeNotice";
import "./SettingsManagement.css";

function announceSaved(value: ManagementSettings) {
    window.dispatchEvent(
        new CustomEvent("tgdrive:settings-saved", {
            detail: {
                admission_paused: value.admission_paused,
                restart_required: value.restart_required,
            },
        }),
    );
}
function ClientHealth({ client }: { client: ManagedClient }) {
    const label = clientConnectionLabel(client);
    return (
        <span
            className={
                "client-health " +
                (client.enabled &&
                client.connected &&
                client.healthy &&
                !["queued", "checking", "waiting", "failed"].includes(
                    client.connection_check ?? "",
                )
                    ? "is-connected"
                    : "")
            }
        >
            <span aria-hidden="true" />
            {label}
        </span>
    );
}
export default function SettingsManagement({
    onNotice,
    demoMode = false,
}: {
    onNotice?: (message: string, error?: boolean) => void;
    demoMode?: boolean;
}) {
    const [settings, setSettings] = useState<ManagementSettings | null>(null);
    const [draft, setDraft] = useState<ConnectionDraft | null>(null);
    const [target, setTarget] = useState<string | null>(null);
    const [premiumOpen, setPremiumOpen] = useState(false);
    const [error, setError] = useState("");
    const [formError, setFormError] = useState("");
    const [notice, setNotice] = useState("");
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const { refreshing: refreshingConnections, run } =
        useManualRefresh(onNotice);
    const manualPending = useRef(false);
    const checksBusy = Boolean(
        settings?.clients.some((client) =>
            ["queued", "checking", "waiting"].includes(
                client.connection_check ?? "",
            ),
        ),
    );
    const checksPending = Boolean(
        settings?.clients.some(
            (client) =>
                ["queued", "checking"].includes(
                    client.connection_check ?? "",
                ) ||
                (client.connection_check === "waiting" &&
                    (!client.connection_retry_at ||
                        client.connection_retry_at * 1000 <= Date.now())),
        ),
    );
    const generation = useRef(0);
    const mounted = useRef(true);
    const editBase = useRef<ManagementSettings | null>(null);
    const firstField = useRef<HTMLInputElement>(null);
    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        if (manualPending.current && !manual) return false;
        const requestId = ++generation.current;
        try {
            const value = await api.settings(signal);
            if (
                mounted.current &&
                !signal?.aborted &&
                requestId === generation.current
            ) {
                setSettings(value);
                setError("");
                return true;
            }
            return false;
        } catch (cause) {
            if (
                mounted.current &&
                requestId === generation.current &&
                !signal?.aborted
            ) {
                setSettings(null);
                setError(errorMessage(cause));
            }
            if (manual) throw cause;
            return false;
        } finally {
            if (mounted.current && requestId === generation.current)
                setLoading(false);
        }
    }, []);
    useEffect(() => {
        mounted.current = true;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            await load(controller.signal);
            if (!controller.signal.aborted) timer = setTimeout(poll, 10000);
        };
        void poll();
        return () => {
            mounted.current = false;
            ++generation.current;
            controller.abort();
            clearTimeout(timer);
        };
    }, [load]);
    useEffect(() => {
        if (!checksPending) return;
        const controller = new AbortController();
        const timer = setTimeout(() => void load(controller.signal), 1000);
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [checksPending, settings, load]);
    const refreshSettings = () =>
        run(async (signal) => {
            manualPending.current = true;
            try {
                return await load(signal, true);
            } finally {
                manualPending.current = false;
            }
        }, "Connection status refreshed.");
    const refreshConnections = () => {
        if (demoMode) return;
        return run(async (signal) => {
            manualPending.current = true;
            const requestId = ++generation.current;
            try {
                const value = await api.refreshConnections(signal);
                if (
                    !mounted.current ||
                    signal.aborted ||
                    requestId !== generation.current
                )
                    return false;
                setSettings(value);
                setError("");
                return true;
            } catch (cause) {
                if (
                    mounted.current &&
                    !signal.aborted &&
                    requestId === generation.current
                )
                    setError(errorMessage(cause));
                throw cause;
            } finally {
                manualPending.current = false;
            }
        }, "Connection checks requested.");
    };
    useEffect(() => {
        firstField.current?.focus();
    }, [target]);
    const uploadItems = useSyncExternalStore(
        transfers.subscribe,
        transfers.snapshot,
    );
    const unfinished = uploadBackupState(uploadItems);
    const recovery = settings?.recovery;
    const cleanRepair = Boolean(
        recovery?.state === "error" &&
        !recovery.recovery_required &&
        recovery.protected_revision === recovery.metadata_revision,
    );
    const jobsPending = Boolean(
        settings &&
        (settings.stats.active_jobs ||
            settings.stats.queued_jobs ||
            settings.stats.failed_jobs),
    );
    const blocked =
        !settings ||
        Boolean(unfinished) ||
        jobsPending ||
        !recovery ||
        (recovery.state !== "protected" && !cleanRepair) ||
        recovery.recovery_required === true ||
        recovery.protected_revision < recovery.metadata_revision;
    const blockedReason =
        unfinished || jobsPending
            ? "Finish or cancel pending transfers before saving connection changes."
            : "Restore the database connection before saving connection changes.";
    const credentialRepair = isCredentialRepair(draft, settings);
    const closeEditor = () => {
        setDraft(null);
        setTarget(null);
        setFormError("");
        editBase.current = null;
    };
    const beginEdit = (id: string) => {
        if (demoMode || !settings) return;
        editBase.current = settings;
        setDraft(createConnectionDraft(settings));
        setTarget(id);
        setNotice("");
        setFormError("");
    };
    const addClient = (kind: "bot" | "user") => {
        if (demoMode || !settings) return;
        const next = createConnectionDraft(settings);
        const key = crypto.randomUUID();
        next.clients.push({
            key,
            label: kind === "bot" ? "Storage Bot" : "My Premium account",
            kind,
            enabled: true,
            credential: "",
            removed: false,
        });
        editBase.current = settings;
        setDraft(next);
        setTarget(key);
        setPremiumOpen(false);
        setNotice("");
        setFormError("");
    };
    const changeClient = (key: string, patch: Partial<ClientDraft>) =>
        setDraft(
            (current) =>
                current && {
                    ...current,
                    clients: current.clients.map((row) =>
                        row.key === key ? { ...row, ...patch } : row,
                    ),
                },
        );
    const dirty = Boolean(
        draft &&
        editBase.current &&
        (draft.apiId !== String(editBase.current.api_id) ||
            draft.apiHash.trim() ||
            draft.clients.some((row) => {
                const prior = editBase.current!.clients.find(
                    (client) => client.id === row.id,
                );
                return (
                    !prior ||
                    row.removed ||
                    row.credential.trim() ||
                    row.label !== prior.label ||
                    row.enabled !== prior.enabled
                );
            })),
    );
    const save = async (event: FormEvent) => {
        event.preventDefault();
        if (demoMode || !draft || saving || !dirty) return;
        if (blocked && !credentialRepair) {
            setFormError(blockedReason);
            return;
        }
        let payload;
        try {
            payload = prepareConnectionUpdate(draft);
        } catch (cause) {
            setFormError(errorMessage(cause));
            return;
        }
        setSaving(true);
        setFormError("");
        setNotice("");
        ++generation.current;
        try {
            const value = await api.saveSettings(payload);
            if (!mounted.current) return;
            ++generation.current;
            setSettings(value);
            announceSaved(value);
            setError("");
            closeEditor();
            setNotice(
                value.restart_required
                    ? "Verified and saved. Restart TGDrive to apply your connection changes."
                    : "Connection verified and saved.",
            );
        } catch (cause) {
            if (!mounted.current) return;
            if (
                cause instanceof ApiError &&
                cause.code === "SETTINGS_CONFLICT"
            ) {
                closeEditor();
                setFormError(
                    "Connection settings changed elsewhere. Review the latest configuration and try again.",
                );
                await load();
            } else setFormError(errorMessage(cause));
        } finally {
            if (mounted.current) setSaving(false);
        }
    };
    const footer = () => (
        <div className="connection-editor-footer">
            {blocked && (
                <p className="management-caption">
                    {credentialRepair
                        ? "You can repair a token or session for the same Telegram account while keeping pending transfers. Other connection changes must wait."
                        : blockedReason}{" "}
                    <button
                        type="button"
                        className="inline-link"
                        disabled={refreshingConnections}
                        onClick={() => void refreshSettings()}
                    >
                        {refreshingConnections && <Spinner />} Check Again
                    </button>
                </p>
            )}
            {settings && draft && settings.revision !== draft.revision && (
                <ErrorNotice message="The saved configuration changed. Cancel and reopen this editor before saving." />
            )}
            <ErrorNotice message={formError} />
            <div className="management-actions">
                {dirty && (
                    <button
                        className="primary-button"
                        disabled={
                            demoMode ||
                            saving ||
                            (blocked && !credentialRepair) ||
                            Boolean(
                                settings &&
                                draft &&
                                settings.revision !== draft.revision,
                            )
                        }
                    >
                        {saving ? <Spinner /> : <Check size={17} />}
                        {saving ? "Verifying…" : "Verify and Save"}
                    </button>
                )}
                <button
                    type="button"
                    className="secondary-button"
                    disabled={saving}
                    onClick={closeEditor}
                >
                    {dirty ? "Cancel Changes" : "Done"}
                </button>
            </div>
            {saving && (
                <p role="status" className="management-caption">
                    Checking accounts and channel access. Your current
                    connection stays active.
                </p>
            )}
        </div>
    );
    const editor = (row: ClientDraft) => (
        <form
            className="connection-editor"
            onSubmit={(event) => void save(event)}
        >
            <fieldset disabled={demoMode || saving}>
                {row.removed ? (
                    <div className="client-removal">
                        <p>
                            {row.label} will be removed after you save and
                            restart.
                        </p>
                        <button
                            type="button"
                            className="text-button"
                            onClick={() =>
                                changeClient(row.key, { removed: false })
                            }
                        >
                            <Undo2 size={16} /> Undo Removal
                        </button>
                    </div>
                ) : (
                    <>
                        <div className="management-fields">
                            <div>
                                <label htmlFor={"client-label-" + row.key}>
                                    Local Label
                                </label>
                                <input
                                    ref={firstField}
                                    id={"client-label-" + row.key}
                                    required
                                    maxLength={80}
                                    value={row.label}
                                    onChange={(event) =>
                                        changeClient(row.key, {
                                            label: event.target.value,
                                        })
                                    }
                                />
                            </div>
                            <div>
                                <label htmlFor={"client-credential-" + row.key}>
                                    {row.id ? "Replace " : ""}
                                    {row.kind === "bot"
                                        ? "Bot Token"
                                        : "Telethon Session"}
                                </label>
                                <input
                                    id={"client-credential-" + row.key}
                                    type="password"
                                    autoComplete="new-password"
                                    maxLength={8192}
                                    required={!row.id}
                                    value={row.credential}
                                    aria-describedby={
                                        "credential-help-" + row.key
                                    }
                                    onChange={(event) =>
                                        changeClient(row.key, {
                                            credential: event.target.value,
                                        })
                                    }
                                />
                                <p
                                    id={"credential-help-" + row.key}
                                    className="management-caption"
                                >
                                    {row.id
                                        ? "Leave blank to keep the saved credential."
                                        : row.kind === "bot"
                                          ? "Paste the token from BotFather. It stays hidden after saving."
                                          : "Import a Telethon StringSession generated locally. Treat it as an account credential."}
                                </p>
                            </div>
                        </div>
                        <div className="connection-editor-options">
                            <label className="management-checkbox">
                                <input
                                    type="checkbox"
                                    checked={row.enabled}
                                    onChange={(event) =>
                                        changeClient(row.key, {
                                            enabled: event.target.checked,
                                        })
                                    }
                                />{" "}
                                Enabled after restart
                            </label>
                            {row.id && (
                                <button
                                    type="button"
                                    className="text-button danger-text"
                                    onClick={() =>
                                        changeClient(row.key, {
                                            removed: true,
                                            credential: "",
                                        })
                                    }
                                >
                                    <X size={16} /> Remove Client
                                </button>
                            )}
                        </div>
                        {row.kind === "bot" && (
                            <p className="management-caption">
                                The bot needs channel administrator permission
                                to post and delete messages.
                            </p>
                        )}
                    </>
                )}
            </fieldset>
            {footer()}
        </form>
    );
    const selected = draft?.clients.find((row) => row.key === target);
    const connectedClients =
        settings?.clients.filter(
            (client) => clientConnectionLabel(client) === "Connected",
        ).length ?? 0;
    return (
        <div
            className="connection-management page-body"
            aria-busy={saving || refreshingConnections}
        >
            {demoMode && <DemoModeNotice />}
            {loading && (
                <div className="loading-state" role="status">
                    <Spinner /> Loading connections…
                </div>
            )}
            {error && (
                <div className="management-load-error">
                    <ErrorNotice message={error} />
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={refreshingConnections}
                        onClick={() => void refreshSettings()}
                    >
                        {refreshingConnections ? (
                            <Spinner />
                        ) : (
                            <RefreshCw size={16} />
                        )}{" "}
                        Try Again
                    </button>
                </div>
            )}
            {notice && (
                <p className="management-notice" role="status">
                    <Check size={18} />
                    {notice}
                </p>
            )}
            {settings?.storage_channel_error && (
                <div className="management-pending" role="alert">
                    <AlertCircle size={20} aria-hidden="true" />
                    <div>
                        <strong>Storage channel needs attention</strong>
                        <p>{settings.storage_channel_error}</p>
                        <p>
                            Resolve the issue above, then restart TGDrive to
                            check the channel again. Keep the same storage
                            channel for this drive.
                        </p>
                    </div>
                </div>
            )}
            {settings?.restart_required && (
                <div className="management-pending" role="status">
                    <AlertCircle size={20} />
                    <div>
                        <strong>Saved changes need a restart</strong>
                        <p>
                            New file changes are paused. Your running connection
                            is still available for browsing and downloads.
                        </p>
                        <details>
                            <summary>How to Restart</summary>
                            <code>docker compose restart app</code>
                            <p>
                                Run this in the installation folder, using the
                                existing Compose project name if you selected
                                one.
                            </p>
                        </details>
                    </div>
                </div>
            )}
            {settings && (
                <>
                    <section
                        className="settings-section"
                        aria-labelledby="storage-clients-heading"
                    >
                        <div className="settings-section-title">
                            <div>
                                <h2 id="storage-clients-heading">
                                    Storage Clients
                                </h2>
                                <p>
                                    Bots store your files. Available Premium
                                    accounts are preferred for upload and
                                    download speed. All accounts use the same
                                    file split size, capped at 2000 MiB per
                                    document.
                                </p>
                                <p className="connection-summary">
                                    {connectedClients} connected ·{" "}
                                    {settings.clients.length} configured
                                </p>
                            </div>
                            <div className="management-actions">
                                <button
                                    type="button"
                                    className="text-button"
                                    disabled={
                                        demoMode ||
                                        refreshingConnections ||
                                        checksBusy ||
                                        Boolean(draft) ||
                                        premiumOpen
                                    }
                                    title="Check saved credentials and storage channel access"
                                    onClick={() => void refreshConnections()}
                                >
                                    {refreshingConnections || checksPending ? (
                                        <Spinner />
                                    ) : (
                                        <RefreshCw size={16} />
                                    )}
                                    {refreshingConnections || checksPending
                                        ? "Checking…"
                                        : checksBusy
                                          ? "Waiting to retry…"
                                          : "Refresh"}
                                </button>
                                <button
                                    type="button"
                                    className="primary-button"
                                    disabled={
                                        demoMode ||
                                        Boolean(draft) ||
                                        premiumOpen ||
                                        settings.clients.length >=
                                            MAX_STORAGE_CLIENTS
                                    }
                                    onClick={() => addClient("bot")}
                                >
                                    <Plus size={17} /> Add Storage Bot
                                </button>
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={
                                        demoMode ||
                                        Boolean(draft) ||
                                        premiumOpen ||
                                        blocked ||
                                        settings.clients.length >=
                                            MAX_STORAGE_CLIENTS
                                    }
                                    onClick={() => {
                                        setPremiumOpen(true);
                                        setNotice("");
                                    }}
                                >
                                    <Plus size={17} /> Connect Premium Account
                                </button>
                            </div>
                        </div>
                        <div
                            className="managed-client-columns"
                            aria-hidden="true"
                        >
                            <span>Account</span>
                            <span>Connection & activity</span>
                            <span />
                        </div>
                        <ul
                            className="managed-client-list"
                            aria-label="Configured storage clients"
                        >
                            {settings.clients.map((client) => (
                                <li
                                    key={client.id}
                                    className={
                                        target === client.id ? "is-editing" : ""
                                    }
                                >
                                    <div className="managed-client-row">
                                        <span className="managed-client-icon">
                                            {client.kind === "bot" ? (
                                                <Bot size={20} />
                                            ) : (
                                                <UserRound size={20} />
                                            )}
                                        </span>
                                        <div className="managed-client-info">
                                            <strong>
                                                {clientDisplayName(client)}
                                            </strong>
                                            <span>
                                                {client.username && (
                                                    <>@{client.username} · </>
                                                )}
                                                {client.kind === "bot"
                                                    ? "Storage Bot"
                                                    : "Premium account"}
                                            </span>
                                        </div>
                                        <div className="managed-client-state">
                                            <ClientHealth client={client} />
                                            <span>
                                                {client.connection_check ===
                                                    "passed" &&
                                                client.active_leases === 0 ? (
                                                    client.kind === "bot" ? (
                                                        "Token and channel verified"
                                                    ) : (
                                                        "Account and channel verified"
                                                    )
                                                ) : (
                                                    <>
                                                        {client.active_leases}{" "}
                                                        active{" "}
                                                        {client.active_leases ===
                                                        1
                                                            ? "operation"
                                                            : "operations"}
                                                    </>
                                                )}
                                            </span>
                                            {(client.connection_check_error ||
                                                client.error) && (
                                                <span
                                                    className="danger-text"
                                                    role="status"
                                                >
                                                    {client.connection_check_error ||
                                                        client.error}
                                                </span>
                                            )}
                                        </div>
                                        <button
                                            type="button"
                                            className="text-button"
                                            aria-label={
                                                "Manage " +
                                                clientDisplayName(client)
                                            }
                                            disabled={
                                                demoMode ||
                                                saving ||
                                                Boolean(
                                                    draft &&
                                                    target !== client.id,
                                                ) ||
                                                premiumOpen
                                            }
                                            onClick={() =>
                                                target === client.id
                                                    ? closeEditor()
                                                    : beginEdit(client.id)
                                            }
                                        >
                                            <Pencil size={16} />
                                            <span>Manage</span>
                                        </button>
                                    </div>
                                    {target === client.id &&
                                        selected &&
                                        editor(selected)}
                                </li>
                            ))}
                            {selected && !selected.id && (
                                <li className="is-editing new-client-editor">
                                    <h3>
                                        {selected.kind === "bot"
                                            ? "Add Storage Bot"
                                            : "Import Premium Session"}
                                    </h3>
                                    {editor(selected)}
                                </li>
                            )}
                        </ul>
                        {!demoMode && premiumOpen && (
                            <PremiumConnection
                                revision={settings.revision}
                                blockedReason={blocked ? blockedReason : ""}
                                onCancel={() => setPremiumOpen(false)}
                                onRefresh={() => void load()}
                                onComplete={(value) => {
                                    ++generation.current;
                                    setSettings(value);
                                    announceSaved(value);
                                    setPremiumOpen(false);
                                    setNotice(
                                        "Premium account verified and saved. Restart TGDrive to use it.",
                                    );
                                }}
                            />
                        )}
                        {!draft && !premiumOpen && blocked && (
                            <p className="management-caption connection-readiness">
                                {blockedReason}{" "}
                                <button
                                    type="button"
                                    className="inline-link"
                                    disabled={refreshingConnections}
                                    onClick={() => void refreshSettings()}
                                >
                                    {refreshingConnections && <Spinner />} Check
                                    Readiness
                                </button>
                            </p>
                        )}
                        {cleanRepair && (
                            <p className="management-caption">
                                The last confirmed catalog is current. You can
                                repair a failed connection; backup needs
                                attention until the repaired service restarts
                                and verifies it.
                            </p>
                        )}
                        {settings.restart_required &&
                            settings.active_clients?.length > 0 && (
                                <details className="management-running">
                                    <summary>
                                        Currently running clients{" "}
                                        <ChevronDown size={15} />
                                    </summary>
                                    <ul>
                                        {settings.active_clients.map(
                                            (client) => (
                                                <li key={client.id}>
                                                    <span>
                                                        {clientDisplayName(
                                                            client,
                                                        )}
                                                    </span>
                                                    <span>
                                                        {clientConnectionLabel({
                                                            ...client,
                                                            enabled: true,
                                                        })}
                                                    </span>
                                                </li>
                                            ),
                                        )}
                                    </ul>
                                </details>
                            )}
                    </section>
                    <section
                        className="settings-section"
                        aria-labelledby="telegram-api-heading"
                    >
                        <div className="settings-section-title">
                            <div>
                                <h2 id="telegram-api-heading">Telegram API</h2>
                                <p>
                                    Application credentials for connecting this
                                    installation to Telegram.
                                </p>
                            </div>
                            <button
                                type="button"
                                className="text-button"
                                disabled={
                                    demoMode ||
                                    saving ||
                                    Boolean(draft && target !== "api") ||
                                    premiumOpen
                                }
                                onClick={() =>
                                    target === "api"
                                        ? closeEditor()
                                        : beginEdit("api")
                                }
                            >
                                <Pencil size={16} /> Edit API Credentials
                            </button>
                        </div>
                        <dl className="connection-api-summary">
                            <div>
                                <dt>API ID</dt>
                                <dd>{settings.api_id}</dd>
                            </div>
                            <div>
                                <dt>API Hash</dt>
                                <dd>
                                    <KeyRound size={15} />
                                    {settings.api_hash_configured
                                        ? "Saved · hidden"
                                        : "Not Configured"}
                                </dd>
                            </div>
                            <div>
                                <dt>Storage Channel</dt>
                                <dd>
                                    {settings.storage_channel}
                                    <small>Fixed for this drive</small>
                                </dd>
                            </div>
                        </dl>
                        {target === "api" && draft && (
                            <form
                                className="connection-editor"
                                onSubmit={(event) => void save(event)}
                            >
                                <fieldset disabled={demoMode || saving}>
                                    <div className="management-fields">
                                        <div>
                                            <label htmlFor="telegram-api-id">
                                                API ID
                                            </label>
                                            <input
                                                id="telegram-api-id"
                                                ref={firstField}
                                                inputMode="numeric"
                                                autoComplete="off"
                                                required
                                                maxLength={10}
                                                value={draft.apiId}
                                                onChange={(event) =>
                                                    setDraft({
                                                        ...draft,
                                                        apiId: event.target
                                                            .value,
                                                    })
                                                }
                                            />
                                        </div>
                                        <div>
                                            <label htmlFor="telegram-api-hash">
                                                Replace API Hash
                                            </label>
                                            <input
                                                id="telegram-api-hash"
                                                type="password"
                                                autoComplete="new-password"
                                                maxLength={32}
                                                value={draft.apiHash}
                                                onChange={(event) =>
                                                    setDraft({
                                                        ...draft,
                                                        apiHash:
                                                            event.target.value,
                                                    })
                                                }
                                            />
                                            <p className="management-caption">
                                                Leave blank to keep the saved
                                                hash.
                                            </p>
                                        </div>
                                    </div>
                                </fieldset>
                                {footer()}
                            </form>
                        )}
                        <details className="connection-advanced">
                            <summary>Advanced Connection Options</summary>
                            <p>
                                Already have an authorized Telethon
                                StringSession?
                            </p>
                            <button
                                type="button"
                                className="secondary-button"
                                disabled={
                                    demoMode ||
                                    Boolean(draft) ||
                                    premiumOpen ||
                                    settings.clients.length >=
                                        MAX_STORAGE_CLIENTS
                                }
                                onClick={() => addClient("user")}
                            >
                                Import session (advanced)
                            </button>
                        </details>
                    </section>
                    <p className="management-caption connection-footer-note">
                        Credentials are saved privately in MongoDB and encrypted
                        in Telegram database backups. Keep at least one enabled
                        bot. Verified changes apply after restart.
                    </p>
                </>
            )}
            {!draft && <ErrorNotice message={formError} />}
        </div>
    );
}
