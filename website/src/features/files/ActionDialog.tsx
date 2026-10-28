import Preview from "../previews/Preview";
import { useEffect, useState } from "react";
import {
    Folder,
    ChevronRight,
    RefreshCw,
    Move,
    Link,
    Copy,
    Download,
    AlertCircle,
    Code,
} from "lucide-react";
import { api, request, ApiError, errorMessage } from "../../shared/api";
import { performanceApi } from "../performance/performanceApi";
import { itemPath, formatTimestamp, type DriveItem } from "../../shared/types";
import { transfers } from "../transfers/transfers";
import { Modal, ErrorNotice, Spinner } from "../../shared/components";
import { embedUrl } from "../shares/embedLinks";
import "./ActionDialog.css";
import ArchiveDialog, { type ArchiveDialogState } from "./ArchiveDialog";
export type ActionDialogState =
    | ArchiveDialogState
    | { type: "folder" }
    | { type: "rename"; item: DriveItem }
    | { type: "url" }
    | { type: "torrent" }
    | { type: "share"; item: DriveItem }
    | { type: "move"; items: DriveItem[] }
    | { type: "delete"; items: DriveItem[] }
    | { type: "restore"; items: DriveItem[] }
    | { type: "preview"; item: DriveItem }
    | { type: "download-link"; item: DriveItem }
    | { type: "prepare"; item: DriveItem };
export default function ActionDialog({
    dialog,
    destination,
    auth,
    onClose,
    onChanged,
    onNotice,
    onDelete,
    onRestore,
}: {
    dialog: ActionDialogState;
    destination: string;
    auth: string;
    onClose: () => void;
    onChanged: () => void;
    onNotice: (message: string, error?: boolean) => void;
    onDelete: (items: DriveItem[]) => Promise<void>;
    onRestore: (items: DriveItem[]) => Promise<void>;
}) {
    const originalName = dialog.type === "rename" ? dialog.item.name : "";
    const extensionStart = originalName.lastIndexOf(".");
    const extension =
        dialog.type === "rename" &&
        dialog.item.type === "file" &&
        extensionStart > 0 &&
        extensionStart < originalName.length - 1
            ? originalName.slice(extensionStart)
            : "";
    const [name, setName] = useState(
        extension ? originalName.slice(0, -extension.length) : originalName,
    );
    const [url, setUrl] = useState("");
    const [compat, setCompat] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [expires, setExpires] = useState("7");
    const [shareLabel, setShareLabel] = useState("");
    const [customExpiry, setCustomExpiry] = useState("");
    const [link, setLink] = useState("");
    const [expiry, setExpiry] = useState<string | null>(null);
    const [requestId, setRequestId] = useState(() =>
        crypto.randomUUID().replaceAll("-", "_"),
    );
    const close = () => {
        if (!busy) onClose();
    };
    const run = async (task: () => Promise<void>) => {
        setBusy(true);
        setError("");
        try {
            await task();
        } catch (error) {
            setError(errorMessage(error));
        } finally {
            setBusy(false);
        }
    };
    if (dialog.type === "archive" || dialog.type === "extract")
        return (
            <ArchiveDialog
                dialog={dialog}
                destination={destination}
                onClose={onClose}
                onNotice={onNotice}
            />
        );
    if (dialog.type === "download-link" || dialog.type === "prepare")
        return (
            <Modal
                title={
                    dialog.type === "prepare"
                        ? "Preload file"
                        : "Copy Download Link"
                }
                onClose={close}
            >
                <p>{dialog.item.name}</p>
                <p>
                    {dialog.type === "prepare"
                        ? "Cache the whole file on the server for faster later streaming and downloads. This uses the shared cache budget and runs behind active streams and downloads. Cached bytes may be removed when space is needed."
                        : "Anyone with this link can download the file without logging in. An active link is reused, or a new one-day link is created. Revoke it in Shared Links."}
                </p>
                {dialog.type === "prepare" && (
                    <>
                        <p>
                            Opening or streaming fetches requested chunks with
                            adaptive, bounded read-ahead. Automatic preloading
                            can warm additional upcoming playback chunks.
                            Downloads fetch their requested ranges. Use this
                            action to preload the entire file in advance. Your
                            device still needs an internet connection to access
                            the server copy.
                        </p>
                        <p>
                            View progress and manage cached files in{" "}
                            <a className="inline-link" href="/?view=preloads">
                                Preloads
                            </a>
                            .
                        </p>
                    </>
                )}
                {link && (
                    <label>
                        Download link
                        <input
                            readOnly
                            value={link}
                            onFocus={(e) => e.currentTarget.select()}
                        />
                    </label>
                )}
                {link && (
                    <p className="muted">
                        {expiry
                            ? "Expires " + formatTimestamp(expiry)
                            : "No expiry. Revoke access in Shared Links."}
                    </p>
                )}
                <ErrorNotice message={error} />
                <div className="dialog-actions">
                    <button
                        className="secondary-button"
                        onClick={close}
                        disabled={busy}
                    >
                        Close
                    </button>
                    <button
                        className="primary-button"
                        disabled={busy}
                        onClick={() =>
                            void run(async () => {
                                if (dialog.type === "prepare") {
                                    const job = await performanceApi.prepare(
                                        itemPath(dialog.item),
                                    );
                                    onNotice(
                                        job.state === "ready"
                                            ? "File is already fully cached on the server. View it in Preloads."
                                            : job.state === "preparing"
                                              ? "File is preloading on the server. View progress in Preloads."
                                              : "Preload queued. View progress in Preloads.",
                                    );
                                    onClose();
                                } else {
                                    const result = await request<{
                                        url: string;
                                        expires_at: string | null;
                                    }>("/api/v1/download-links", {
                                        path: itemPath(dialog.item),
                                    });
                                    setLink(result.url);
                                    setExpiry(result.expires_at);
                                    onChanged();
                                    await navigator.clipboard.writeText(
                                        result.url,
                                    );
                                    onNotice("Download link copied.");
                                }
                            })
                        }
                    >
                        {busy ? <Spinner /> : null}
                        {dialog.type === "prepare"
                            ? "Preload file"
                            : "Create or Copy Link"}
                    </button>
                </div>
            </Modal>
        );
    if (dialog.type === "move")
        return (
            <MoveDialog
                items={dialog.items}
                onClose={onClose}
                onChanged={onChanged}
            />
        );
    if (dialog.type === "preview")
        return <Preview item={dialog.item} auth={auth} onClose={onClose} />;
    if (dialog.type === "restore")
        return (
            <Modal title="Restore to a Shared Folder?" onClose={close}>
                <p>
                    The destination has an active public folder link. Restoring
                    these items will make them accessible to anyone with that
                    link. Previously revoked item links stay revoked.
                </p>
                <ErrorNotice message={error} />
                <div className="dialog-actions">
                    <button
                        className="secondary-button"
                        onClick={close}
                        disabled={busy}
                    >
                        Cancel
                    </button>
                    <button
                        className="primary-button"
                        disabled={busy}
                        onClick={() =>
                            void run(async () => {
                                await onRestore(dialog.items);
                                onChanged();
                            })
                        }
                    >
                        {busy ? <Spinner /> : null} Restore and Allow Public
                        Access
                    </button>
                </div>
            </Modal>
        );
    if (dialog.type === "delete")
        return (
            <Modal title="Delete Permanently?" onClose={close}>
                <p>
                    This will remove{" "}
                    {dialog.items.length === 1
                        ? "“" + dialog.items[0].name + "”"
                        : dialog.items.length + " selected items"}{" "}
                    and all files inside from TGDrive’s database. The original
                    files will remain in Telegram.
                </p>
                <p className="muted">
                    Items disappear from Trash once deletion starts and are
                    removed in the background. You cannot restore them after
                    deletion. Failed items reappear in Trash so you can retry.
                </p>
                <ErrorNotice message={error} />
                <div className="dialog-actions">
                    <button
                        className="secondary-button"
                        disabled={busy}
                        onClick={onClose}
                    >
                        Keep in Trash
                    </button>
                    <button
                        className="danger-button"
                        disabled={busy}
                        onClick={() =>
                            void run(async () => {
                                await onDelete(dialog.items);
                                onChanged();
                            })
                        }
                    >
                        {busy ? (
                            <>
                                <Spinner /> Deleting…
                            </>
                        ) : (
                            "Delete Permanently"
                        )}
                    </button>
                </div>
            </Modal>
        );
    if (dialog.type === "share")
        return (
            <Modal title={"Share “" + dialog.item.name + "”"} onClose={close}>
                <p>
                    Anyone with the link can{" "}
                    {dialog.item.type === "folder"
                        ? "view and download this folder’s current and future contents"
                        : "download this file"}
                    . They cannot edit your drive.
                </p>
                {!link && (
                    <p className="muted">
                        Each link has its own expiry. Existing links keep
                        working.
                    </p>
                )}
                {!link ? (
                    <form
                        onSubmit={(event) => {
                            event.preventDefault();
                            void run(async () => {
                                const result = await api.share(
                                    itemPath(dialog.item),
                                    expires === "never" || expires === "custom"
                                        ? null
                                        : Number(expires),
                                    shareLabel,
                                    expires === "custom"
                                        ? new Date(customExpiry).toISOString()
                                        : undefined,
                                );
                                const params = new URLSearchParams({
                                    path:
                                        dialog.item.type === "folder"
                                            ? "/share_" + dialog.item.id
                                            : itemPath(dialog.item),
                                    auth: result.auth,
                                });
                                setLink(
                                    window.location.origin +
                                        (dialog.item.type === "folder"
                                            ? "/"
                                            : "/file") +
                                        "?" +
                                        params,
                                );
                                setExpiry(result.expires_at);
                            });
                        }}
                    >
                        <label htmlFor="share-label">
                            Link label (optional)
                        </label>
                        <input
                            id="share-label"
                            maxLength={80}
                            value={shareLabel}
                            placeholder="For example, Alex or project review"
                            onChange={(event) =>
                                setShareLabel(event.target.value)
                            }
                        />
                        <fieldset className="share-expiry-options">
                            <legend>Link Expires</legend>
                            <div>
                                {[
                                    ["1", "1 day"],
                                    ["7", "7 days"],
                                    ["30", "30 days"],
                                    ["never", "Never"],
                                    ["custom", "Custom"],
                                ].map(([value, label]) => (
                                    <button
                                        type="button"
                                        key={value}
                                        aria-pressed={expires === value}
                                        onClick={() => setExpires(value)}
                                    >
                                        {label}
                                    </button>
                                ))}
                            </div>
                        </fieldset>
                        {expires === "custom" && (
                            <>
                                <label htmlFor="share-custom-expiry">
                                    Expiry date and time
                                </label>
                                <input
                                    id="share-custom-expiry"
                                    type="datetime-local"
                                    required
                                    value={customExpiry}
                                    onChange={(event) =>
                                        setCustomExpiry(event.target.value)
                                    }
                                />
                            </>
                        )}
                        <ErrorNotice message={error} />
                        <div className="dialog-actions">
                            <button
                                className="secondary-button"
                                type="button"
                                onClick={close}
                            >
                                Cancel
                            </button>
                            <button className="primary-button" disabled={busy}>
                                {busy ? <Spinner /> : <Link size={16} />} Create
                                Share Link
                            </button>
                        </div>
                    </form>
                ) : (
                    <>
                        <label htmlFor="share-link">Share Link</label>
                        <input
                            id="share-link"
                            value={link}
                            readOnly
                            onFocus={(event) => event.target.select()}
                        />
                        <label htmlFor="embed-link">Embed Link</label>
                        <input
                            id="embed-link"
                            value={embedUrl(link)}
                            readOnly
                            onFocus={(event) => event.target.select()}
                        />
                        <p className="muted">
                            Use the embed link as an iframe source on your
                            website. It has the same access and expiry as this
                            share link.
                        </p>
                        <p className="muted">
                            {expiry
                                ? "Expires " + formatTimestamp(expiry) + "."
                                : "This link does not expire."}{" "}
                            Revoke access from Shared Links.
                        </p>
                        <ErrorNotice message={error} />
                        <div className="dialog-actions share-dialog-actions">
                            <button
                                className="secondary-button"
                                onClick={close}
                            >
                                Done
                            </button>
                            <button
                                className="secondary-button"
                                onClick={() =>
                                    void run(async () => {
                                        if (!navigator.clipboard)
                                            throw new Error(
                                                "Clipboard access is unavailable. Select and copy the embed link above.",
                                            );
                                        await navigator.clipboard.writeText(
                                            embedUrl(link),
                                        );
                                        onNotice("Embed Link copied.");
                                    })
                                }
                            >
                                <Code size={16} aria-hidden="true" /> Copy Embed
                                Link
                            </button>
                            <button
                                className="primary-button"
                                onClick={() =>
                                    void run(async () => {
                                        if (!navigator.clipboard)
                                            throw new Error(
                                                "Clipboard access is unavailable. Select and copy the link above.",
                                            );
                                        await navigator.clipboard.writeText(
                                            link,
                                        );
                                        onNotice("Share Link copied.");
                                    })
                                }
                            >
                                <Copy size={16} /> Copy Link
                            </button>
                        </div>
                    </>
                )}
            </Modal>
        );
    const isFolder = dialog.type === "folder";
    const isRename = dialog.type === "rename";
    const isTorrent = dialog.type === "torrent";
    return (
        <Modal
            title={
                isFolder
                    ? "New Folder"
                    : isRename
                      ? "Rename"
                      : isTorrent
                        ? "Import torrent"
                        : "Upload from URL"
            }
            onClose={close}
        >
            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    void run(async () => {
                        if (isFolder) {
                            await api.folder(destination, name);
                            onNotice("Folder created.");
                            onChanged();
                        } else if (isRename) {
                            await api.rename(
                                itemPath(dialog.item),
                                name + extension,
                            );
                            onNotice("Name updated.");
                            onChanged();
                        } else if (isTorrent) {
                            transfers.startTorrent(url, destination, requestId);
                            onNotice(
                                "Starting torrent import. Follow progress in Transfers.",
                            );
                            onClose();
                        } else {
                            transfers.startRemote(
                                url,
                                destination,
                                compat,
                                requestId,
                            );
                            onNotice(
                                "Starting URL import. Follow progress in Transfers.",
                            );
                            onClose();
                        }
                    });
                }}
            >
                {isFolder || isRename ? (
                    <>
                        <label htmlFor="item-name">
                            {isFolder ? "Folder Name" : "Name"}
                        </label>
                        <div className="item-name-field">
                            <input
                                id="item-name"
                                required
                                maxLength={255 - extension.length}
                                value={name}
                                aria-describedby={
                                    extension ? "item-name-hint" : undefined
                                }
                                onChange={(event) =>
                                    setName(event.target.value)
                                }
                            />
                            {extension && (
                                <span
                                    className="rename-extension"
                                    aria-hidden="true"
                                >
                                    {extension}
                                </span>
                            )}
                        </div>
                        {extension && (
                            <p id="item-name-hint" className="muted">
                                The {extension} extension will stay the same.
                            </p>
                        )}
                    </>
                ) : (
                    <>
                        <p className="muted">
                            {isTorrent
                                ? "Paste a magnet link or a public .torrent URL. Every file is imported, with folders kept intact."
                                : "Import a direct HTTP or HTTPS file link. The server downloads it and uploads the original bytes to Telegram."}
                        </p>
                        <label htmlFor="remote-url">
                            {isTorrent
                                ? "Magnet link or torrent URL"
                                : "Direct File URL"}
                        </label>
                        <input
                            id="remote-url"
                            type={isTorrent ? "text" : "url"}
                            required
                            autoComplete="off"
                            spellCheck={false}
                            aria-describedby={
                                isTorrent ? "torrent-import-hint" : undefined
                            }
                            placeholder={
                                isTorrent
                                    ? "magnet:?xt=urn:btih:…"
                                    : "https://example.com/file.zip"
                            }
                            value={url}
                            onChange={(event) => {
                                setUrl(event.target.value);
                                setRequestId(
                                    crypto.randomUUID().replaceAll("-", "_"),
                                );
                            }}
                        />
                        {isTorrent ? (
                            <p id="torrent-import-hint" className="muted">
                                Saves to the current folder. You can close this
                                tab after the import starts; TGDrive must stay
                                running.
                            </p>
                        ) : (
                            <label className="check-field">
                                <input
                                    type="checkbox"
                                    checked={compat}
                                    onChange={(event) =>
                                        setCompat(event.target.checked)
                                    }
                                />
                                <span>
                                    Compatibility Mode
                                    <small>
                                        Use one connection. Unsupported sources
                                        fall back automatically.
                                    </small>
                                </span>
                            </label>
                        )}
                    </>
                )}
                <ErrorNotice message={error} />
                <div className="dialog-actions">
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={busy}
                        onClick={onClose}
                    >
                        Cancel
                    </button>
                    <button className="primary-button" disabled={busy}>
                        {busy ? <Spinner /> : null}
                        {isFolder
                            ? "Create Folder"
                            : isRename
                              ? "Save Name"
                              : "Start Import"}
                    </button>
                </div>
            </form>
        </Modal>
    );
}
function MoveDialog({
    items,
    onClose,
    onChanged,
}: {
    items: DriveItem[];
    onClose: () => void;
    onChanged: () => void;
}) {
    const [path, setPath] = useState("/");
    const [folders, setFolders] = useState<DriveItem[]>([]);
    const [trail, setTrail] = useState([{ path: "/", name: "My Files" }]);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [conflict, setConflict] = useState("keep_both");
    const [exposure, setExposure] = useState(false);
    const [allow, setAllow] = useState(false);
    useEffect(() => {
        let ignore = false;
        setLoading(true);
        setFolders([]);
        setError("");
        setExposure(false);
        setAllow(false);
        void api
            .directory(path)
            .then((result) => {
                if (!ignore) {
                    setError("");
                    setFolders(
                        Object.values(result.data.contents).filter(
                            (item) =>
                                item.type === "folder" &&
                                !items.some(
                                    (selected) =>
                                        itemPath(item) === itemPath(selected) ||
                                        itemPath(item).startsWith(
                                            itemPath(selected) + "/",
                                        ),
                                ),
                        ),
                    );
                }
            })
            .catch((error) => {
                if (!ignore) setError(errorMessage(error));
            })
            .finally(() => {
                if (!ignore) setLoading(false);
            });
        return () => {
            ignore = true;
        };
    }, [path, items]);
    const move = async () => {
        setBusy(true);
        setError("");
        try {
            const result = await api.move(
                items.map(itemPath),
                path,
                conflict,
                allow,
            );
            const failed = result.results?.filter(
                (result) => result.error || result.status === "error",
            );
            if (failed?.length)
                throw new Error(
                    failed.length +
                        " item(s) could not be moved. " +
                        (failed[0].error || "Refresh and try again."),
                );
            onChanged();
        } catch (error) {
            if (
                error instanceof ApiError &&
                error.code.includes("PUBLIC_EXPOSURE")
            ) {
                setExposure(true);
                setError(
                    "This move would make some files accessible through a public folder link. Confirm below to continue.",
                );
            } else setError(errorMessage(error));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal
            title={
                "Move " +
                (items.length === 1
                    ? "“" + items[0].name + "”"
                    : items.length + " items")
            }
            onClose={() => {
                if (!busy) onClose();
            }}
        >
            <p className="muted">
                Choose a different destination folder. Existing files are never
                overwritten.
            </p>
            <nav
                className="move-breadcrumbs"
                aria-label="Destination breadcrumb"
            >
                {trail.map((crumb, index) => (
                    <button
                        key={crumb.path}
                        className="text-button"
                        onClick={() => {
                            setPath(crumb.path);
                            setTrail(trail.slice(0, index + 1));
                        }}
                    >
                        {index > 0 && <ChevronRight size={13} />} {crumb.name}
                    </button>
                ))}
            </nav>
            <div className="folder-picker" aria-busy={loading}>
                {loading ? (
                    <p>
                        <Spinner /> Loading folders…
                    </p>
                ) : folders.length ? (
                    folders.map((folder) => (
                        <button
                            key={folder.id}
                            onClick={() => {
                                const next = itemPath(folder);
                                setPath(next);
                                setTrail([
                                    ...trail,
                                    { path: next, name: folder.name },
                                ]);
                            }}
                        >
                            <Folder size={19} />
                            <span>{folder.name}</span>
                            <ChevronRight size={17} />
                        </button>
                    ))
                ) : (
                    <p className="muted">No folders inside this destination.</p>
                )}
            </div>
            <fieldset className="dialog-choice-options">
                <legend>Name Conflicts</legend>
                <div>
                    <button
                        type="button"
                        aria-pressed={conflict === "keep_both"}
                        onClick={() => setConflict("keep_both")}
                    >
                        Keep Both
                    </button>
                    <button
                        type="button"
                        aria-pressed={conflict === "skip"}
                        onClick={() => setConflict("skip")}
                    >
                        Skip Existing
                    </button>
                </div>
                <p className="muted">
                    {conflict === "keep_both"
                        ? "Add a number to the moved item if its name is already used."
                        : "Leave items with matching names in their original location."}
                </p>
            </fieldset>
            <ErrorNotice message={error} />
            {exposure && (
                <label className="check-field">
                    <input
                        type="checkbox"
                        checked={allow}
                        onChange={(event) => setAllow(event.target.checked)}
                    />
                    <span>
                        I understand that files moved here may become public.
                    </span>
                </label>
            )}
            <div className="dialog-actions">
                <button
                    className="secondary-button"
                    onClick={onClose}
                    disabled={busy}
                >
                    Cancel
                </button>
                <button
                    className="primary-button"
                    disabled={
                        busy ||
                        loading ||
                        items.every((item) => (item.path || "/") === path) ||
                        (exposure && !allow)
                    }
                    onClick={() => void move()}
                >
                    {busy ? <Spinner /> : <Move size={16} />} Move Here
                </button>
            </div>
        </Modal>
    );
}
