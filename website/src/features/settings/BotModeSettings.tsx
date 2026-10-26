import { useCallback, useEffect, useState } from "react";
import { Button, Paper } from "@mantine/core";
import { ChevronRight, Folder } from "lucide-react";
import { api, errorMessage, type BotUploadSettings } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import { itemPath, type DriveItem } from "../../shared/types";
import {
    useManualRefresh,
    type RefreshNotice,
} from "../../shared/useManualRefresh";
import "./BotModeSettings.css";

export default function BotModeSettings({
    onNotice,
    demoMode = false,
}: { onNotice?: RefreshNotice; demoMode?: boolean } = {}) {
    const [saved, setSaved] = useState<BotUploadSettings | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [picking, setPicking] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const manual = useManualRefresh(onNotice);
    const load = useCallback(async (signal?: AbortSignal, explicit = false) => {
        setLoading(true);
        setError("");
        try {
            const value = await api.botUploadSettings(signal);
            if (signal?.aborted) return false;
            setSaved(value);
            return true;
        } catch (cause) {
            if (!signal?.aborted) setError(errorMessage(cause));
            if (explicit) throw cause;
            return false;
        } finally {
            if (!signal?.aborted) setLoading(false);
        }
    }, []);
    useEffect(() => {
        const controller = new AbortController();
        void load(controller.signal);
        return () => controller.abort();
    }, [load]);
    useEffect(() => {
        if (saved && window.location.hash === "#bot-mode")
            document
                .getElementById("bot-mode")
                ?.scrollIntoView({ block: "start" });
    }, [saved]);
    const save = async (path: string | null) => {
        if (demoMode) return;
        setBusy(true);
        setError("");
        setNotice("");
        try {
            setSaved(await api.saveBotUploadSettings(path));
            setPicking(false);
            setNotice(
                "Bot upload folder saved. Applies to the next file you send.",
            );
        } catch (cause) {
            setError(errorMessage(cause));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Paper
            component="section"
            id="bot-mode"
            className="settings-section preference-section"
            aria-labelledby="bot-mode-heading"
        >
            <h2 id="bot-mode-heading">Bot uploads</h2>
            <p className="settings-help">
                Files you send or forward to the Telegram bot are saved in this
                folder.
            </p>
            {loading ? (
                <p role="status">
                    <Spinner /> Loading bot settings…
                </p>
            ) : (
                saved && (
                    <>
                        <p className="bot-upload-destination">
                            <span>Upload folder</span>
                            <strong>{saved.folder_label}</strong>
                        </p>
                        {!saved.available && (
                            <ErrorNotice message="This folder is no longer available. Choose another before sending more files to the bot." />
                        )}
                        {!saved.enabled && (
                            <p className="settings-help">
                                Bot mode is not enabled on this installation.
                                You can still choose its upload folder.
                            </p>
                        )}
                        <div className="upload-settings-actions">
                            <Button
                                className="secondary-button"
                                disabled={demoMode || busy}
                                onClick={() => {
                                    setPicking(true);
                                    setError("");
                                    setNotice("");
                                }}
                            >
                                Change folder
                            </Button>
                            <Button
                                variant="subtle"
                                className="text-button"
                                disabled={demoMode || busy}
                                onClick={() => void save(null)}
                            >
                                Use default folder
                            </Button>
                        </div>
                        <p className="settings-help">
                            Default: My Files / TGDriveBotModeFiles. Created
                            automatically on the first bot upload. Changes apply
                            without restarting.
                        </p>
                    </>
                )
            )}
            {!picking && <ErrorNotice message={error} />}
            {!saved && (!loading || manual.refreshing) && (
                <Button
                    variant="subtle"
                    disabled={loading || manual.refreshing}
                    aria-busy={manual.refreshing}
                    onClick={() =>
                        void manual.run(
                            (signal) => load(signal, true),
                            "Bot settings refreshed.",
                        )
                    }
                >
                    {manual.refreshing && <Spinner />}
                    {manual.refreshing ? "Refreshing…" : "Try again"}
                </Button>
            )}
            {notice && <p role="status">{notice}</p>}
            {picking && (
                <FolderPicker
                    busy={busy}
                    error={error}
                    onSave={save}
                    onClose={() => {
                        if (!busy) setPicking(false);
                    }}
                />
            )}
        </Paper>
    );
}

function FolderPicker({
    busy,
    error,
    onSave,
    onClose,
}: {
    busy: boolean;
    error: string;
    onSave: (path: string) => Promise<void>;
    onClose: () => void;
}) {
    const [trail, setTrail] = useState([{ path: "/", name: "My Files" }]);
    const [folders, setFolders] = useState<DriveItem[]>([]);
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState("");
    const [retry, setRetry] = useState(0);
    const path = trail[trail.length - 1].path;
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        setLoadError("");
        setFolders([]);
        void api
            .directory(path, "", controller.signal)
            .then((result) => {
                if (!controller.signal.aborted)
                    setFolders(
                        Object.values(result.data.contents)
                            .filter((item) => item.type === "folder")
                            .sort((a, b) => a.name.localeCompare(b.name)),
                    );
            })
            .catch((cause) => {
                if (!controller.signal.aborted)
                    setLoadError(errorMessage(cause));
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [path, retry]);
    return (
        <Modal title="Choose bot upload folder" onClose={onClose}>
            <p className="muted">
                Open a folder, then choose Use this folder. All bot uploads will
                go there.
            </p>
            <nav
                className="move-breadcrumbs"
                aria-label="Bot upload folder breadcrumb"
            >
                {trail.map((crumb, index) => (
                    <button
                        type="button"
                        key={crumb.path}
                        className="text-button"
                        disabled={busy}
                        onClick={() => setTrail(trail.slice(0, index + 1))}
                    >
                        {index > 0 && (
                            <ChevronRight size={13} aria-hidden="true" />
                        )}
                        {crumb.name}
                    </button>
                ))}
            </nav>
            <div className="folder-picker" aria-busy={loading}>
                {loading ? (
                    <p role="status">
                        <Spinner /> Loading folders…
                    </p>
                ) : loadError ? (
                    <>
                        <ErrorNotice message={loadError} />
                        <button
                            type="button"
                            className="text-button"
                            onClick={() => setRetry(retry + 1)}
                        >
                            Try again
                        </button>
                    </>
                ) : folders.length ? (
                    folders.map((folder) => (
                        <button
                            type="button"
                            key={folder.id}
                            disabled={busy}
                            onClick={() =>
                                setTrail([
                                    ...trail,
                                    {
                                        path: itemPath(folder),
                                        name: folder.name,
                                    },
                                ])
                            }
                        >
                            <Folder size={19} aria-hidden="true" />
                            <span>{folder.name}</span>
                            <ChevronRight size={16} aria-hidden="true" />
                        </button>
                    ))
                ) : (
                    <p className="muted">
                        No subfolders here. You can use this folder.
                    </p>
                )}
            </div>
            <ErrorNotice message={error} />
            <div className="dialog-actions">
                <button
                    type="button"
                    className="secondary-button"
                    disabled={busy}
                    onClick={onClose}
                >
                    Cancel
                </button>
                <button
                    type="button"
                    className="primary-button"
                    disabled={busy || loading || Boolean(loadError)}
                    onClick={() => void onSave(path)}
                >
                    {busy ? "Saving…" : "Use this folder"}
                </button>
            </div>
        </Modal>
    );
}
