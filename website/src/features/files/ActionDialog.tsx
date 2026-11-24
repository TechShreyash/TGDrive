import Preview from "../previews/Preview";
import type { VideoProgressMode } from "../previews/video/videoWatchProgress";
import { useEffect, useRef, useState } from "react";
import { NativeSelect, PasswordInput } from "@mantine/core";
import {
    Folder,
    ArrowUp,
    ChevronRight,
    RefreshCw,
    Move,
    Link,
    Copy,
    Download,
    AlertCircle,
    Code,
    FolderPlus,
    Globe,
    Magnet,
    Pencil,
    Trash2,
    ArchiveRestore,
    HardDriveDownload,
} from "lucide-react";
import { api, request, ApiError, errorMessage } from "../../shared/api";
import { performanceApi } from "../performance/performanceApi";
import {
    itemPath,
    formatTimestamp,
    dateTimeInputToISO,
    getTimezone,
    type DriveItem,
} from "../../shared/types";
import { transfers } from "../transfers/transfers";
import { Modal, ErrorNotice, FileIcon, Spinner } from "../../shared/components";
import { embedUrl } from "../shares/embedLinks";
import { shareUrl } from "../shares/SharesView";
import "./ActionDialog.css";
import ArchiveDialog, { type ArchiveDialogState } from "./ArchiveDialog";
import PdfPasswordDialog from "./PdfPasswordDialog";
export type ActionDialogState =
    | ArchiveDialogState
    | { type: "pdf-password"; item: DriveItem }
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
    destinationName = "My Files",
    auth,
    videoProgressMode,
    onClose,
    onChanged,
    onNotice,
    onDelete,
    onRestore,
}: {
    dialog: ActionDialogState;
    destination: string;
    destinationName?: string;
    auth: string;
    videoProgressMode?: VideoProgressMode;
    onClose: () => void;
    onChanged: () => void;
    onNotice: (message: string, error?: boolean) => void;
    onDelete: (items: DriveItem[]) => void;
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
    const renameFocused = useRef(false);
    const [url, setUrl] = useState("");
    const [compat, setCompat] = useState(false);
    const [busy, setBusy] = useState(false);
    const running = useRef(false);
    const [error, setError] = useState("");
    const [expires, setExpires] = useState("7");
    const [shareLabel, setShareLabel] = useState("");
    const [sharePassword, setSharePassword] = useState("");
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
        if (running.current) return;
        running.current = true;
        setBusy(true);
        setError("");
        try {
            await task();
        } catch (error) {
            setError(errorMessage(error));
        } finally {
            running.current = false;
            setBusy(false);
        }
    };
    if (dialog.type === "pdf-password")
        return (
            <PdfPasswordDialog
                item={dialog.item}
                onNotice={onNotice}
                onClose={onClose}
            />
        );
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
                icon={
                    dialog.type === "prepare" ? <HardDriveDownload /> : <Link />
                }
                tone="blue"
                description={dialog.item.name}
                onClose={close}
            >
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
                                    let downloadLink = link;
                                    if (!downloadLink) {
                                        const result = await request<{
                                            url: string;
                                            expires_at: string | null;
                                        }>("/api/v1/download-links", {
                                            path: itemPath(dialog.item),
                                        });
                                        downloadLink = result.url;
                                        setLink(downloadLink);
                                        setExpiry(result.expires_at);
                                        onChanged();
                                    }
                                    try {
                                        await navigator.clipboard.writeText(
                                            downloadLink,
                                        );
                                    } catch {
                                        throw new Error(
                                            "Could not copy automatically. Select and copy the download link above.",
                                        );
                                    }
                                    onNotice("Download link copied.");
                                }
                            })
                        }
                    >
                        {busy ? <Spinner /> : null}
                        {dialog.type === "prepare"
                            ? "Preload file"
                            : link
                              ? "Copy Link"
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
        return (
            <Preview
                item={dialog.item}
                auth={auth}
                videoProgressMode={videoProgressMode}
                onClose={onClose}
            />
        );
    if (dialog.type === "restore")
        return (
            <Modal
                title="Restore to a Shared Folder?"
                icon={<ArchiveRestore />}
                tone="orange"
                onClose={close}
            >
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
            <Modal
                title="Delete Permanently?"
                icon={<Trash2 />}
                tone="red"
                description={
                    <>
                        This will remove{" "}
                        {dialog.items.length === 1
                            ? "“" + dialog.items[0].name + "”"
                            : dialog.items.length + " selected items"}{" "}
                        and all files inside from TGDrive’s database. The
                        original files will remain in Telegram.
                    </>
                }
                onClose={close}
            >
                <p className="muted">
                    Items disappear from Trash once deletion starts and are
                    removed in the background. You cannot restore them after
                    deletion. Failed items reappear in Trash so you can retry.
                </p>
                <div className="dialog-actions">
                    <button className="secondary-button" onClick={onClose}>
                        Keep in Trash
                    </button>
                    <button
                        className="danger-button"
                        onClick={() => onDelete(dialog.items)}
                    >
                        Delete Permanently
                    </button>
                </div>
            </Modal>
        );
    if (dialog.type === "share") {
        const item = dialog.item;
        const isSharedFolder = item.type === "folder";
        const copyLink = (url: string, embed = false) =>
            void run(async () => {
                if (!navigator.clipboard)
                    throw new Error(
                        embed
                            ? "Clipboard access is unavailable. Select and copy the embed link above."
                            : "Clipboard access is unavailable. Select and copy the link above.",
                    );
                await navigator.clipboard.writeText(url);
                onNotice(embed ? "Embed Link copied." : "Share Link copied.");
            });

        return (
            <Modal
                title={link ? "Share link ready" : "Share link"}
                onClose={close}
            >
                <div className="share-target">
                    <FileIcon name={item.name} folder={isSharedFolder} />
                    <div>
                        <strong>{item.name}</strong>
                        <p>
                            {isSharedFolder
                                ? "Recipients can view and download current and future files in this folder."
                                : "Recipients can view and download this file."}
                        </p>
                    </div>
                </div>
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
                                        ? dateTimeInputToISO(customExpiry)
                                        : undefined,
                                    sharePassword || undefined,
                                );
                                setLink(
                                    shareUrl(
                                        result.auth,
                                        dialog.item.id,
                                        dialog.item.type === "folder",
                                    ),
                                );
                                setExpiry(result.expires_at);
                                setSharePassword("");
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
                        <PasswordInput
                            label="Share password (optional)"
                            value={sharePassword}
                            maxLength={255}
                            autoComplete="new-password"
                            onChange={(event) =>
                                setSharePassword(event.currentTarget.value)
                            }
                        />
                        <fieldset className="share-expiry-options">
                            <legend>Link expiry</legend>
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
                                    Expiry date and time ({getTimezone()})
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
                        <div className="share-result">
                            <label htmlFor="share-link">Share Link</label>
                            <div className="share-result-control">
                                <input
                                    id="share-link"
                                    value={link}
                                    readOnly
                                    autoFocus
                                    onFocus={(event) => event.target.select()}
                                />
                                <button
                                    className="primary-button"
                                    onClick={() => copyLink(link)}
                                >
                                    <Copy size={16} aria-hidden="true" /> Copy
                                    Link
                                </button>
                            </div>
                        </div>
                        {isSharedFolder && (
                            <div className="share-result">
                                <label htmlFor="embed-link">Embed Link</label>
                                <div className="share-result-control">
                                    <input
                                        id="embed-link"
                                        value={embedUrl(link)}
                                        readOnly
                                        onFocus={(event) =>
                                            event.target.select()
                                        }
                                    />
                                    <button
                                        className="secondary-button"
                                        onClick={() =>
                                            copyLink(embedUrl(link), true)
                                        }
                                    >
                                        <Code size={16} aria-hidden="true" />{" "}
                                        Copy Embed Link
                                    </button>
                                </div>
                                <p className="muted">
                                    Use this link as an iframe source. It shares
                                    the same access and expiry as the share
                                    link.
                                </p>
                            </div>
                        )}
                        {!isSharedFolder && (
                            <p className="muted share-result-hint">
                                This link also works as an iframe source.
                            </p>
                        )}
                        <p className="muted share-result-expiry">
                            {expiry
                                ? "Expires " + formatTimestamp(expiry) + "."
                                : "This link does not expire."}{" "}
                            Revoke access from Shared Links.
                        </p>
                        <ErrorNotice message={error} />
                        <div className="dialog-actions">
                            <button
                                className="secondary-button"
                                onClick={close}
                            >
                                Done
                            </button>
                        </div>
                    </>
                )}
            </Modal>
        );
    }
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
            icon={
                isFolder ? (
                    <FolderPlus />
                ) : isRename ? (
                    <Pencil />
                ) : isTorrent ? (
                    <Magnet />
                ) : (
                    <Globe />
                )
            }
            tone={
                isFolder
                    ? "amber"
                    : isRename
                      ? "green"
                      : isTorrent
                        ? "violet"
                        : "blue"
            }
            description={
                isFolder
                    ? "Create a folder in " + destinationName + "."
                    : isRename
                      ? dialog.item.name
                      : isTorrent
                        ? "Paste a magnet link or a public .torrent URL. You can choose files after TGDrive reads the torrent."
                        : "Import a direct HTTP or HTTPS file link. The server downloads it and uploads the original bytes to Telegram."
            }
            onClose={close}
        >
            <form
                noValidate={!isFolder && !isRename}
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
                                onFocus={(event) => {
                                    if (!isRename || renameFocused.current)
                                        return;
                                    renameFocused.current = true;
                                    event.currentTarget.select();
                                }}
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
                        <p className="import-destination">
                            Save to <strong>{destinationName}</strong>
                        </p>
                        <label htmlFor="remote-url">
                            {isTorrent
                                ? "Magnet link or torrent URL"
                                : "Direct File URL"}
                        </label>
                        <div className="input-with-icon">
                            {isTorrent ? (
                                <Magnet size={18} aria-hidden="true" />
                            ) : (
                                <Link size={18} aria-hidden="true" />
                            )}
                            <input
                                id="remote-url"
                                type={isTorrent ? "text" : "url"}
                                required
                                autoComplete="off"
                                spellCheck={false}
                                aria-describedby={
                                    isTorrent
                                        ? "torrent-import-hint"
                                        : undefined
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
                                        crypto
                                            .randomUUID()
                                            .replaceAll("-", "_"),
                                    );
                                }}
                            />
                        </div>
                        {isTorrent ? (
                            <p id="torrent-import-hint" className="muted">
                                You can close this tab after the import starts.
                                TGDrive must stay running.
                            </p>
                        ) : (
                            <label className="check-field option-row">
                                <input
                                    type="checkbox"
                                    className="switch"
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
    const sameParent = items.every(
        (item) => (item.path || "/") === (items[0]?.path || "/"),
    );
    const initialPath = sameParent ? items[0]?.path || "/" : "/";
    const [path, setPath] = useState(initialPath);
    const [folders, setFolders] = useState<DriveItem[]>([]);
    const [trail, setTrail] = useState(() => [
        { path: "/", name: "My Files" },
        ...(initialPath === "/"
            ? []
            : [
                  {
                      path: initialPath,
                      name: items[0]?.parent_names?.at(-1) || "Current folder",
                  },
              ]),
    ]);
    const [source, setSource] = useState(() => [
        "My Files",
        ...(initialPath === "/"
            ? []
            : items[0]?.parent_names || ["Current folder"]),
    ]);
    const [loading, setLoading] = useState(true);
    const [listingFailed, setListingFailed] = useState(false);
    const [destinationMissing, setDestinationMissing] = useState(false);
    const [listingAttempt, setListingAttempt] = useState(0);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [conflict, setConflict] = useState("keep_both");
    const [exposure, setExposure] = useState(false);
    const [allow, setAllow] = useState(false);
    const parent = trail.at(-2);
    const atSource = sameParent && initialPath === path;
    const destinationCrumb = useRef<HTMLButtonElement>(null);
    const focusDestination = useRef(false);
    const selectDestination = (next: typeof trail) => {
        setLoading(true);
        setFolders([]);
        setError("");
        setAllow(false);
        setExposure(false);
        const nextPath = next.at(-1)?.path ?? "/";
        if (nextPath === path) setListingAttempt((attempt) => attempt + 1);
        else focusDestination.current = true;
        setPath(nextPath);
        setTrail(next);
    };
    useEffect(() => {
        if (!focusDestination.current) return;
        focusDestination.current = false;
        destinationCrumb.current?.focus({ preventScroll: true });
    }, [path]);
    useEffect(() => {
        if (path !== initialPath || initialPath === "/") return;
        let ignore = false;
        void api
            .nodes({ parent_id: initialPath, limit: 1, type: "folder" })
            .then((result) => {
                if (!ignore && result.folder?.breadcrumbs) {
                    setSource([
                        "My Files",
                        ...result.folder.breadcrumbs
                            .slice(1)
                            .map((crumb) => crumb.name),
                    ]);
                    setTrail([
                        { path: "/", name: "My Files" },
                        ...result.folder.breadcrumbs.slice(1).map((crumb) => ({
                            path: crumb.path,
                            name: crumb.name,
                        })),
                    ]);
                }
            })
            .catch(() => {});
        return () => {
            ignore = true;
        };
    }, [initialPath, path]);
    useEffect(() => {
        let ignore = false;
        setListingFailed(false);
        setDestinationMissing(false);
        setExposure(false);
        setAllow(false);
        void api
            .folders(path)
            .then((result) => {
                if (!ignore) {
                    setError("");
                    setFolders(
                        result.filter(
                            (item) =>
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
                if (!ignore) {
                    setListingFailed(true);
                    const missing =
                        error instanceof ApiError && error.status === 404;
                    setDestinationMissing(missing);
                    setError(
                        missing
                            ? "This destination is no longer available."
                            : errorMessage(error),
                    );
                }
            })
            .finally(() => {
                if (!ignore) setLoading(false);
            });
        return () => {
            ignore = true;
        };
    }, [path, items, listingAttempt]);
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
            } else if (error instanceof ApiError && error.status === 404) {
                try {
                    await api.nodes({
                        parent_id: path,
                        limit: 1,
                        type: "folder",
                    });
                    setError(errorMessage(error));
                } catch (checkError) {
                    if (
                        checkError instanceof ApiError &&
                        checkError.status === 404
                    ) {
                        setListingFailed(true);
                        setDestinationMissing(true);
                        setFolders([]);
                        setExposure(false);
                        setAllow(false);
                        setError("This destination is no longer available.");
                    } else setError(errorMessage(error));
                }
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
            initialFocus=".move-up, .move-breadcrumbs [aria-current]"
            onClose={() => {
                if (!busy) onClose();
            }}
        >
            <dl className="move-source">
                <dt>From</dt>
                <dd>{sameParent ? source.join(" / ") : "Different folders"}</dd>
            </dl>
            <div className="move-destination-label">Destination</div>
            <nav
                className="move-breadcrumbs"
                aria-label="Destination breadcrumb"
            >
                {trail.map((crumb, index) => (
                    <button
                        key={crumb.path}
                        ref={
                            index === trail.length - 1
                                ? destinationCrumb
                                : undefined
                        }
                        className="text-button"
                        aria-current={
                            index === trail.length - 1 ? "location" : undefined
                        }
                        disabled={busy}
                        onClick={() => {
                            if (crumb.path !== path)
                                selectDestination(trail.slice(0, index + 1));
                        }}
                    >
                        {index > 0 && (
                            <ChevronRight size={13} aria-hidden="true" />
                        )}
                        <span>{crumb.name}</span>
                    </button>
                ))}
            </nav>
            {parent && (
                <button
                    type="button"
                    className="text-button move-up"
                    disabled={busy}
                    onClick={() => selectDestination(trail.slice(0, -1))}
                >
                    <ArrowUp size={16} aria-hidden="true" />
                    <span>Up to {parent.name}</span>
                </button>
            )}
            {atSource && !loading && folders.length > 0 && (
                <p className="move-folder-status muted">
                    These items are already here. Choose another folder.
                </p>
            )}
            <div
                className={
                    folders.length || loading
                        ? "folder-picker move-folders"
                        : "move-folder-status"
                }
                aria-busy={loading}
            >
                {loading ? (
                    <p>
                        <Spinner /> Loading folders…
                    </p>
                ) : listingFailed ? (
                    <ErrorNotice message={error} />
                ) : folders.length ? (
                    folders.map((folder) => (
                        <button
                            key={folder.id}
                            disabled={busy}
                            onClick={() =>
                                selectDestination([
                                    ...trail,
                                    {
                                        path: itemPath(folder),
                                        name: folder.name,
                                    },
                                ])
                            }
                        >
                            <Folder size={19} />
                            <span>{folder.name}</span>
                            <ChevronRight size={17} />
                        </button>
                    ))
                ) : (
                    <p className="muted">
                        {atSource
                            ? "These items are already here. Choose another folder."
                            : "This folder has no subfolders."}
                    </p>
                )}
            </div>
            {!listingFailed && (
                <NativeSelect
                    className="move-conflict"
                    label="If a name already exists"
                    value={conflict}
                    disabled={busy}
                    data={[
                        { value: "keep_both", label: "Keep both" },
                        { value: "skip", label: "Skip existing" },
                    ]}
                    description={
                        conflict === "keep_both"
                            ? "Add a number to the moved item’s name."
                            : "Leave matching items in their original folder."
                    }
                    onChange={(event) => setConflict(event.currentTarget.value)}
                />
            )}
            {!listingFailed && <ErrorNotice message={error} />}
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
                {listingFailed && !loading ? (
                    <button
                        type="button"
                        className="primary-button"
                        onClick={() => {
                            selectDestination(
                                destinationMissing ? trail.slice(0, 1) : trail,
                            );
                            if (!destinationMissing)
                                destinationCrumb.current?.focus();
                        }}
                    >
                        {destinationMissing && path !== "/"
                            ? "Choose another folder"
                            : "Try again"}
                    </button>
                ) : (
                    <button
                        className="primary-button"
                        disabled={
                            busy || loading || atSource || (exposure && !allow)
                        }
                        onClick={() => void move()}
                    >
                        {busy ? <Spinner /> : <Move size={16} />} Move Here
                    </button>
                )}
            </div>
        </Modal>
    );
}
