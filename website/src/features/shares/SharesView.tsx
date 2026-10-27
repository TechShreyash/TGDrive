import { useEffect, useRef, useState } from "react";
import {
    Link,
    Copy,
    FolderOpen,
    RefreshCw,
    X,
    Check,
    Pencil,
    Code,
} from "lucide-react";
import { api, errorMessage, request } from "../../shared/api";
import { useManualRefresh } from "../../shared/useManualRefresh";
import {
    formatDate,
    formatTimestamp,
    getTimezone,
    type Share,
} from "../../shared/types";
import { FileIcon, Spinner, ErrorNotice, Modal } from "../../shared/components";
import { embedUrl } from "./embedLinks";
import "./SharesView.css";

export function shareExpired(share: Share) {
    return (
        share.expires_at != null &&
        new Date(
            typeof share.expires_at === "number"
                ? share.expires_at * 1000
                : share.expires_at,
        ).getTime() <= Date.now()
    );
}
export function shareCopyIssue(share: Share) {
    if (!share.id) return "Refresh Shared Links before managing this entry.";
    if (shareExpired(share)) return "This link has expired.";
    if (!share.copyable)
        return "Original link is unavailable; existing copies still work.";
    return "";
}
function createdTime(share: Share) {
    const date = new Date(share.created_at);
    return Number.isNaN(date.getTime())
        ? "—"
        : new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeStyle: "medium",
              timeZone: getTimezone(),
          }).format(date);
}
function expiryDescription(share: Share) {
    return share.expires_at == null
        ? "No expiry"
        : `${shareExpired(share) ? "Expired" : "Expires"} ${formatTimestamp(share.expires_at)}`;
}
function shareDescription(share: Share) {
    return [
        share.name || "Shared Item",
        share.label,
        expiryDescription(share),
        "Created " + formatTimestamp(share.created_at),
    ]
        .filter(Boolean)
        .join(". ");
}
export function shareUrl(auth: string, nodeId: string, folder: boolean) {
    return (
        window.location.origin +
        (folder ? "/" : "/file") +
        "?" +
        new URLSearchParams({
            path: folder ? "/share_" + nodeId : "/" + nodeId,
            auth,
        })
    );
}
export default function SharesView({
    refresh,
    onError,
    onChange,
    onLocate,
    onNotice,
    query = "",
}: {
    refresh: number;
    onError: (message: string) => void;
    onChange: () => void;
    onLocate: (share: Share) => void;
    onNotice?: (message: string, error?: boolean) => void;
    query?: string;
}) {
    const [shares, setShares] = useState<Share[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [filter, setFilter] = useState<"active" | "expired" | "all">(
        "active",
    );
    const generation = useRef(0);
    const manualPending = useRef(false);
    const { refreshing, run } = useManualRefresh(onNotice);
    const [busy, setBusy] = useState<{
        id: string;
        action: "copy" | "embed" | "revoke";
    } | null>(null);
    const [copied, setCopied] = useState("");
    const [confirm, setConfirm] = useState<Share | null>(null);
    const [revokeError, setRevokeError] = useState("");
    const [manualLink, setManualLink] = useState<{
        url: string;
        embed: boolean;
    } | null>(null);
    const [editing, setEditing] = useState<Share | null>(null);
    // Expiry changes while this page is open, without a database refresh.
    const [, tick] = useState(0);
    useEffect(() => {
        const timer = setInterval(() => tick((n) => n + 1), 30000);
        return () => clearInterval(timer);
    }, []);
    useEffect(() => {
        if (manualPending.current) return;
        const controller = new AbortController();
        const requestId = ++generation.current;
        setLoading(true);
        void request<{ shares: Share[] }>("/api/v1/shares", undefined, {
            signal: controller.signal,
        })
            .then((result) => {
                if (
                    !controller.signal.aborted &&
                    requestId === generation.current
                ) {
                    setShares(result.shares);
                    setError("");
                }
            })
            .catch((cause) => {
                if (
                    !controller.signal.aborted &&
                    requestId === generation.current
                )
                    setError(errorMessage(cause));
            })
            .finally(() => {
                if (
                    !controller.signal.aborted &&
                    requestId === generation.current
                )
                    setLoading(false);
            });
        return () => controller.abort();
    }, [refresh]);
    const refreshShares = () =>
        run(async (signal) => {
            manualPending.current = true;
            const requestId = ++generation.current;
            try {
                const result = await request<{ shares: Share[] }>(
                    "/api/v1/shares",
                    undefined,
                    {
                        signal,
                    },
                );
                if (signal.aborted || requestId !== generation.current)
                    return false;
                setShares(result.shares);
                setError("");
                setLoading(false);
                return true;
            } catch (cause) {
                if (!signal.aborted && requestId === generation.current)
                    setError(errorMessage(cause));
                throw cause;
            } finally {
                manualPending.current = false;
            }
        }, "Shared links refreshed.");
    useEffect(() => {
        if (!copied) return;
        const timer = setTimeout(() => setCopied(""), 2500);
        return () => clearTimeout(timer);
    }, [copied]);
    const matching = shares.filter((share) =>
        ((share.name || "Shared Item") + " " + (share.label || ""))
            .normalize("NFC")
            .toLocaleLowerCase()
            .includes(query.normalize("NFC").toLocaleLowerCase()),
    );
    const active = matching.filter((share) => !shareExpired(share));
    const expired = matching.filter(shareExpired);
    const visible = (
        filter === "active" ? active : filter === "expired" ? expired : matching
    )
        .slice()
        .sort(
            (a, b) =>
                Number(shareExpired(a)) - Number(shareExpired(b)) ||
                Date.parse(b.created_at) - Date.parse(a.created_at),
        );
    const revoke = async (share: Share) => {
        if (busy) return;
        if (!share.id) {
            onError("Refresh Shared Links before managing this entry.");
            return;
        }
        setBusy({ id: share.id, action: "revoke" });
        setRevokeError("");
        try {
            await api.revoke(share.path, share.id);
            setConfirm(null);
            onChange();
        } catch (cause) {
            if (confirm) setRevokeError(errorMessage(cause));
            else onError(errorMessage(cause));
        } finally {
            setBusy(null);
        }
    };
    const copy = async (share: Share, embed = false) => {
        if (busy) return;
        const issue = shareCopyIssue(share);
        if (issue) return;
        setBusy({ id: share.id!, action: embed ? "embed" : "copy" });
        setCopied("");
        try {
            const result = await api.shareLink(share.path, share.id);
            const link = shareUrl(
                result.auth,
                result.node_id,
                result.type === "folder",
            );
            const url = embed ? embedUrl(link) : link;
            try {
                await navigator.clipboard.writeText(url);
                setCopied((embed ? "embed:" : "") + share.id!);
            } catch {
                setManualLink({ url, embed });
            }
        } catch (cause) {
            onError(errorMessage(cause));
        } finally {
            setBusy(null);
        }
    };
    const locate = async (share: Share) => {
        try {
            if (!share.id)
                throw new Error(
                    "Refresh Shared Links before managing this entry.",
                );
            const latest = (await api.shares()).shares.find(
                (row) => row.id === share.id,
            );
            if (!latest)
                throw new Error(
                    "This shared item is no longer available. Refresh the list.",
                );
            onLocate(latest);
        } catch (cause) {
            onError(errorMessage(cause));
        }
    };
    const copiedShare = shares.find(
        (share) => share.id === copied || "embed:" + share.id === copied,
    );
    return (
        <div className="shares-page" aria-busy={loading || refreshing}>
            <div className="shares-toolbar">
                <div
                    className="shares-filters"
                    role="group"
                    aria-label="Filter Shared Links"
                >
                    {(["active", "expired", "all"] as const).map((value) => (
                        <button
                            key={value}
                            className="text-button"
                            aria-pressed={filter === value}
                            onClick={() => setFilter(value)}
                        >
                            {
                                {
                                    active: "Active",
                                    expired: "Expired",
                                    all: "All",
                                }[value]
                            }
                            <span>
                                {value === "all"
                                    ? matching.length
                                    : value === "active"
                                      ? active.length
                                      : expired.length}
                            </span>
                        </button>
                    ))}
                </div>
                <button
                    className="icon-button"
                    aria-label="Refresh Shared Links"
                    title="Refresh"
                    disabled={loading || refreshing}
                    onClick={() => void refreshShares()}
                >
                    {refreshing ? <Spinner /> : <RefreshCw size={18} />}
                </button>
            </div>
            {error && (
                <div className="page-load-error">
                    <ErrorNotice message={error} />
                    <button
                        className="secondary-button"
                        disabled={refreshing}
                        onClick={() => void refreshShares()}
                    >
                        {refreshing && <Spinner />} Try Again
                    </button>
                </div>
            )}
            {loading && !shares.length ? (
                <div className="loading-state" role="status">
                    <Spinner /> Loading shared links…
                </div>
            ) : error && !shares.length ? null : !visible.length ? (
                <div className="empty-state">
                    <Link size={36} />
                    <h2>
                        {query
                            ? "No Matching Shared Links"
                            : filter === "expired"
                              ? "No Expired Links"
                              : filter === "all"
                                ? "No Shared Links"
                                : "No Active Shared Links"}
                    </h2>
                    <p>
                        {query
                            ? "Try another name or clear the search."
                            : filter === "expired"
                              ? "Links that reach their expiry date will appear here."
                              : "Use an item’s Share Link action in My Files to share it."}
                    </p>
                </div>
            ) : (
                <>
                    <div className="share-columns" aria-hidden="true">
                        <span>Shared item</span>
                        <span>Link dates</span>
                        <span>Actions</span>
                    </div>
                    <ul className="shares-rows" aria-label="Shared Links">
                        {visible.map((share) => {
                            const ended = shareExpired(share);
                            const name = share.name || "Shared Item";
                            const copyIssue = shareCopyIssue(share);
                            const revoking =
                                busy?.id === share.id &&
                                busy?.action === "revoke";
                            const detailsId = share.id
                                ? `share-details-${share.id}`
                                : undefined;
                            const description = shareDescription(share);
                            const expiryDate = new Date(
                                typeof share.expires_at === "number"
                                    ? share.expires_at * 1000
                                    : (share.expires_at ?? ""),
                            );
                            return (
                                <li
                                    key={
                                        share.id ||
                                        `${share.path}:${share.created_at}`
                                    }
                                    className="share-row"
                                    data-share-id={share.id}
                                    data-status={ended ? "expired" : "active"}
                                    onDoubleClick={(event) => {
                                        if (
                                            !(
                                                event.target as HTMLElement
                                            ).closest("button,a")
                                        )
                                            void locate(share);
                                    }}
                                >
                                    <FileIcon
                                        name={name}
                                        folder={share.type === "folder"}
                                    />
                                    <div className="share-item">
                                        <button
                                            className="share-name"
                                            title={name}
                                            aria-describedby={detailsId}
                                            onClick={() => void locate(share)}
                                        >
                                            {name}
                                        </button>
                                        <span
                                            className="share-location"
                                            title={[
                                                share.label,
                                                share.folder_label ||
                                                    "My Files",
                                            ]
                                                .filter(Boolean)
                                                .join(" · ")}
                                        >
                                            {share.label && (
                                                <>
                                                    <span className="share-link-label">
                                                        {share.label}
                                                    </span>
                                                    {" · "}
                                                </>
                                            )}
                                            {share.folder_label || "My Files"}
                                        </span>
                                        <span
                                            className="sr-only"
                                            id={detailsId}
                                        >
                                            {description}
                                        </span>
                                    </div>
                                    <div className="share-dates">
                                        <div
                                            className={
                                                "share-expiry" +
                                                (ended ? " ended" : "")
                                            }
                                        >
                                            {share.expires_at ? (
                                                <time
                                                    dateTime={
                                                        Number.isFinite(
                                                            expiryDate.getTime(),
                                                        )
                                                            ? expiryDate.toISOString()
                                                            : undefined
                                                    }
                                                    tabIndex={0}
                                                    aria-label={`${ended ? "Expired" : "Expires"} ${formatTimestamp(share.expires_at)}`}
                                                >
                                                    <span className="share-expiry-label">
                                                        {ended
                                                            ? "Expired"
                                                            : "Expires"}
                                                    </span>
                                                    {formatDate(
                                                        share.expires_at,
                                                    )}
                                                    <span
                                                        className="share-expiry-tooltip"
                                                        role="tooltip"
                                                    >
                                                        {formatTimestamp(
                                                            share.expires_at,
                                                        )}
                                                    </span>
                                                </time>
                                            ) : (
                                                "No expiry"
                                            )}
                                        </div>
                                        <time
                                            className="share-created"
                                            dateTime={share.created_at}
                                            title={
                                                "Created " +
                                                formatTimestamp(
                                                    share.created_at,
                                                )
                                            }
                                        >
                                            Created {createdTime(share)}
                                        </time>
                                    </div>
                                    <div className="share-row-actions">
                                        {[false, true].map((embed) => {
                                            const copying =
                                                busy?.id === share.id &&
                                                busy?.action ===
                                                    (embed ? "embed" : "copy");
                                            const wasCopied =
                                                !!share.id &&
                                                copied ===
                                                    (embed ? "embed:" : "") +
                                                        share.id;
                                            const linkName = embed
                                                ? "embed link"
                                                : "link";
                                            const copyTitle = copying
                                                ? "Copying " + linkName
                                                : wasCopied
                                                  ? (embed
                                                        ? "Embed link"
                                                        : "Link") + " copied"
                                                  : "Copy " + linkName;
                                            return (
                                                <span
                                                    key={linkName}
                                                    className={
                                                        embed
                                                            ? "share-embed-control"
                                                            : "share-copy-control"
                                                    }
                                                    data-unavailable={
                                                        !!copyIssue
                                                    }
                                                    role={
                                                        copyIssue
                                                            ? "group"
                                                            : undefined
                                                    }
                                                    tabIndex={
                                                        copyIssue
                                                            ? 0
                                                            : undefined
                                                    }
                                                    aria-label={
                                                        copyIssue
                                                            ? `Copy ${linkName} unavailable for ${share.label || name}. ${copyIssue}`
                                                            : undefined
                                                    }
                                                    aria-describedby={
                                                        copyIssue
                                                            ? detailsId
                                                            : undefined
                                                    }
                                                    onClick={
                                                        copyIssue
                                                            ? (event) =>
                                                                  event.currentTarget.focus()
                                                            : undefined
                                                    }
                                                    onKeyDown={(event) => {
                                                        if (
                                                            event.key ===
                                                            "Escape"
                                                        )
                                                            event.currentTarget.blur();
                                                    }}
                                                >
                                                    <button
                                                        className={
                                                            "icon-button " +
                                                            (embed
                                                                ? "share-embed-copy"
                                                                : "share-copy")
                                                        }
                                                        disabled={
                                                            !!busy ||
                                                            !!copyIssue
                                                        }
                                                        aria-disabled={
                                                            !!copyIssue
                                                        }
                                                        aria-busy={copying}
                                                        aria-label={`${copyTitle} for ${share.label || name}${copyIssue ? ". " + copyIssue : ""}`}
                                                        aria-describedby={
                                                            detailsId
                                                        }
                                                        title={
                                                            copyIssue
                                                                ? undefined
                                                                : copyTitle
                                                        }
                                                        onClick={() =>
                                                            void copy(
                                                                share,
                                                                embed,
                                                            )
                                                        }
                                                    >
                                                        {copying ? (
                                                            <Spinner />
                                                        ) : wasCopied ? (
                                                            <Check
                                                                size={18}
                                                                aria-hidden="true"
                                                            />
                                                        ) : embed ? (
                                                            <Code
                                                                size={18}
                                                                aria-hidden="true"
                                                            />
                                                        ) : (
                                                            <Copy
                                                                size={18}
                                                                aria-hidden="true"
                                                            />
                                                        )}
                                                    </button>
                                                    {copyIssue && (
                                                        <span
                                                            className="share-copy-explanation"
                                                            role="tooltip"
                                                        >
                                                            {copyIssue}
                                                        </span>
                                                    )}
                                                </span>
                                            );
                                        })}
                                        {share.id && (
                                            <button
                                                className="icon-button"
                                                disabled={!!busy}
                                                title="Edit link"
                                                aria-label={
                                                    "Edit link " +
                                                    (share.label ||
                                                        "for " + name)
                                                }
                                                aria-describedby={detailsId}
                                                onClick={() =>
                                                    setEditing(share)
                                                }
                                            >
                                                <Pencil
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                            </button>
                                        )}
                                        <button
                                            className="icon-button share-locate"
                                            title="Show in folder"
                                            aria-label={
                                                "Show " + name + " in folder"
                                            }
                                            aria-describedby={detailsId}
                                            onClick={() => void locate(share)}
                                        >
                                            <FolderOpen
                                                size={18}
                                                aria-hidden="true"
                                            />
                                        </button>
                                        <button
                                            className="icon-button"
                                            disabled={!!busy || !share.id}
                                            aria-busy={revoking}
                                            title={
                                                ended
                                                    ? "Remove expired link"
                                                    : "Revoke link"
                                            }
                                            aria-label={
                                                (ended
                                                    ? "Remove Expired Link for "
                                                    : "Revoke Link for ") + name
                                            }
                                            aria-describedby={detailsId}
                                            onClick={() => (
                                                setRevokeError(""),
                                                setConfirm(share)
                                            )}
                                        >
                                            {revoking ? (
                                                <Spinner />
                                            ) : (
                                                <X
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                            )}
                                        </button>
                                    </div>
                                </li>
                            );
                        })}
                    </ul>
                </>
            )}
            <span className="sr-only" role="status">
                {busy?.action === "copy" || busy?.action === "embed"
                    ? "Copying " +
                      (busy.action === "embed" ? "embed" : "share") +
                      " link"
                    : copiedShare
                      ? (copied.startsWith("embed:") ? "Embed" : "Share") +
                        " link copied. " +
                        shareDescription(copiedShare)
                      : ""}
            </span>
            {confirm && (
                <Modal
                    title={
                        shareExpired(confirm)
                            ? "Remove Expired Link?"
                            : "Revoke This Link?"
                    }
                    onClose={() => {
                        if (!busy) setConfirm(null);
                    }}
                >
                    <ShareIdentity share={confirm} />
                    <p>
                        {shareExpired(confirm)
                            ? "Remove this expired entry from Shared Links."
                            : "People with this link will lose access."}{" "}
                        Other links are unaffected.
                    </p>
                    <ErrorNotice message={revokeError} />
                    <div className="dialog-actions">
                        <button
                            className="secondary-button"
                            disabled={!!busy}
                            onClick={() => setConfirm(null)}
                        >
                            Cancel
                        </button>
                        <button
                            className="danger-button"
                            disabled={!!busy}
                            onClick={() => void revoke(confirm)}
                        >
                            {busy ? (
                                <Spinner />
                            ) : (
                                <X size={16} aria-hidden="true" />
                            )}
                            {shareExpired(confirm)
                                ? "Remove Entry"
                                : "Revoke Link"}
                        </button>
                    </div>
                </Modal>
            )}
            {editing && (
                <EditShare
                    share={editing}
                    onClose={() => setEditing(null)}
                    onSaved={() => {
                        setEditing(null);
                        onChange();
                    }}
                />
            )}
            {manualLink && (
                <Modal
                    title={
                        manualLink.embed ? "Copy Embed Link" : "Copy Share Link"
                    }
                    onClose={() => setManualLink(null)}
                >
                    <p>Select and copy the link below.</p>
                    <label htmlFor="existing-share-url">
                        {manualLink.embed ? "Embed Link" : "Share Link"}
                    </label>
                    <input
                        id="existing-share-url"
                        readOnly
                        value={manualLink.url}
                        onFocus={(event) => event.target.select()}
                    />
                    <div className="dialog-actions">
                        <button
                            className="primary-button"
                            onClick={() => setManualLink(null)}
                        >
                            Done
                        </button>
                    </div>
                </Modal>
            )}
        </div>
    );
}

function ShareIdentity({ share }: { share: Share }) {
    return (
        <div className="share-dialog-identity">
            <strong>{share.name || "Shared Item"}</strong>
            {share.label && <span>{share.label}</span>}
            <span>{expiryDescription(share)}</span>
            <span>Created {formatTimestamp(share.created_at)}</span>
        </div>
    );
}

function EditShare({
    share,
    onClose,
    onSaved,
}: {
    share: Share;
    onClose: () => void;
    onSaved: () => void;
}) {
    const [label, setLabel] = useState(share.label || "");
    const date = share.expires_at
        ? new Date(
              typeof share.expires_at === "number"
                  ? share.expires_at * 1000
                  : share.expires_at,
          )
        : null;
    const localDate = date
        ? new Date(date.getTime() - date.getTimezoneOffset() * 60000)
              .toISOString()
              .slice(0, 16)
        : "";
    const [expiry, setExpiry] = useState(localDate);
    const [never, setNever] = useState(!share.expires_at);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const save = async () => {
        if (busy) return;
        setBusy(true);
        setError("");
        try {
            const changes: {
                share_id: string;
                label?: string;
                expires_at?: string | null;
            } = { share_id: share.id! };
            if (label !== (share.label || "")) changes.label = label;
            if (never !== !share.expires_at || (!never && expiry !== localDate))
                changes.expires_at = never
                    ? null
                    : new Date(expiry).toISOString();
            await request("/api/v1/shares", changes, { method: "PATCH" });
            onSaved();
        } catch (cause) {
            setError(errorMessage(cause));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal
            title="Edit share link"
            onClose={() => {
                if (!busy) onClose();
            }}
        >
            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    void save();
                }}
            >
                <ShareIdentity share={share} />
                <label htmlFor="edit-share-label">Link label</label>
                <input
                    id="edit-share-label"
                    maxLength={80}
                    value={label}
                    onChange={(event) => setLabel(event.target.value)}
                />
                <label className="checkbox-label">
                    <input
                        type="checkbox"
                        checked={never}
                        onChange={(event) => setNever(event.target.checked)}
                    />
                    No expiry
                </label>
                {!never && (
                    <>
                        <label htmlFor="edit-share-expiry">
                            Expiry date and time
                        </label>
                        <input
                            id="edit-share-expiry"
                            type="datetime-local"
                            required
                            value={expiry}
                            onChange={(event) => setExpiry(event.target.value)}
                        />
                    </>
                )}
                <p className="muted">
                    Changes apply only to this link. Anyone with the link can
                    use it.
                </p>
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
                        {busy && <Spinner />}Save changes
                    </button>
                </div>
            </form>
        </Modal>
    );
}
