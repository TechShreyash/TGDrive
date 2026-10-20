import { useEffect, useState } from "react";
import { Link, Copy, FolderOpen, RefreshCw, X, Check } from "lucide-react";
import { api, errorMessage } from "./api";
import {
  formatLocationLabel,
  formatDate,
  formatTimestamp,
  type Share,
  type DriveItem,
} from "./types";
import { FileIcon, Spinner, ErrorNotice, Modal } from "./components";
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
  onShare,
  query = "",
}: {
  refresh: number;
  onError: (message: string) => void;
  onChange: () => void;
  onLocate: (share: Share) => void;
  onShare: (item: DriveItem) => void;
  query?: string;
}) {
  const [shares, setShares] = useState<Share[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<"active" | "expired" | "all">("active");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState("");
  const [copied, setCopied] = useState("");
  const [confirm, setConfirm] = useState<Share | null>(null);
  const [revokeError, setRevokeError] = useState("");
  const [manualLink, setManualLink] = useState("");
  // Expiry changes while this page is open, without a database refresh.
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 30000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let ignore = false;
    setLoading(true);
    void api
      .shares()
      .then((result) => {
        if (!ignore) {
          setShares(result.shares);
          setError("");
        }
      })
      .catch((cause) => {
        if (!ignore) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [refresh, revision]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(""), 2500);
    return () => clearTimeout(timer);
  }, [copied]);
  const matching = shares.filter((share) =>
    (share.name || "Shared Item")
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
    setBusy(share.path);
    setRevokeError("");
    try {
      await api.revoke(share.path);
      setConfirm(null);
      onChange();
    } catch (cause) {
      if (confirm) setRevokeError(errorMessage(cause));
      else onError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  };
  const copy = async (share: Share) => {
    setBusy(share.path);
    try {
      const result = await api.shareLink(share.path);
      const url = shareUrl(
        result.auth,
        result.node_id,
        result.type === "folder",
      );
      try {
        await navigator.clipboard.writeText(url);
        setCopied(share.path);
      } catch {
        setManualLink(url);
      }
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  };
  const locate = async (share: Share) => {
    try {
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
  const replace = (share: Share) =>
    onShare({
      id: share.node_id || share.path.split("/").pop() || "",
      path:
        share.folder_path ||
        share.path.slice(0, share.path.lastIndexOf("/")) ||
        "/",
      name: share.name || "Shared Item",
      type: share.type || "file",
      size: share.size,
    });
  return (
    <div className="shares-page" aria-busy={loading}>
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
              {{ active: "Active", expired: "Expired", all: "All" }[value]}
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
          disabled={loading}
          onClick={() => setRevision((n) => n + 1)}
        >
          <RefreshCw size={18} />
        </button>
      </div>
      {error && (
        <div className="page-load-error">
          <ErrorNotice message={error} />
          <button
            className="secondary-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Try Again
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
            <span>Expires</span>
            <span>Actions</span>
          </div>
          <ul className="shares-rows" aria-label="Shared Links">
            {visible.map((share) => {
              const ended = shareExpired(share);
              const name = share.name || "Shared Item";
              const expiryDate = new Date(
                typeof share.expires_at === "number"
                  ? share.expires_at * 1000
                  : (share.expires_at ?? ""),
              );
              return (
                <li
                  key={share.id || share.path}
                  className="share-row"
                  data-status={ended ? "expired" : "active"}
                  onDoubleClick={(event) => {
                    if (!(event.target as HTMLElement).closest("button,a"))
                      void locate(share);
                  }}
                >
                  <FileIcon name={name} folder={share.type === "folder"} />
                  <div className="share-item">
                    <button
                      className="share-name"
                      title={name}
                      onClick={() => void locate(share)}
                    >
                      {name}
                    </button>
                    <span
                      className="share-location"
                      title={
                        formatLocationLabel(share.folder_label) || "My Files"
                      }
                    >
                      {formatLocationLabel(share.folder_label) || "My Files"}
                    </span>
                  </div>
                  <div className={"share-expiry" + (ended ? " ended" : "")}>
                    {share.expires_at ? (
                      <time
                        dateTime={
                          Number.isFinite(expiryDate.getTime())
                            ? expiryDate.toISOString()
                            : undefined
                        }
                        tabIndex={0}
                        aria-label={`${ended ? "Expired" : "Expires"} ${formatTimestamp(share.expires_at)}`}
                      >
                        <span className="share-expiry-label">
                          {ended ? "Expired" : "Expires"}
                        </span>
                        {formatDate(share.expires_at)}
                        <span className="share-expiry-tooltip" role="tooltip">
                          {formatTimestamp(share.expires_at)}
                        </span>
                      </time>
                    ) : (
                      "No expiry"
                    )}
                  </div>
                  <div className="share-row-actions">
                    {ended || !share.copyable ? (
                      <button
                        className="secondary-button share-copy"
                        onClick={() => replace(share)}
                        title={
                          ended
                            ? "Create a new link"
                            : "Older link cannot be recovered; replace it to copy a new link"
                        }
                      >
                        <RefreshCw size={15} />
                        {ended ? "Renew Link" : "Replace Link"}
                      </button>
                    ) : (
                      <button
                        className="secondary-button share-copy"
                        disabled={busy === share.path}
                        onClick={() => void copy(share)}
                      >
                        {busy === share.path ? (
                          <Spinner />
                        ) : copied === share.path ? (
                          <Check size={15} />
                        ) : (
                          <Copy size={15} />
                        )}
                        {copied === share.path ? "Copied" : "Copy Link"}
                      </button>
                    )}
                    <button
                      className="icon-button"
                      title="Show in Folder"
                      aria-label={"Show " + name + " in folder"}
                      onClick={() => void locate(share)}
                    >
                      <FolderOpen size={18} />
                    </button>
                    <button
                      className="icon-button"
                      disabled={busy === share.path}
                      title={ended ? "Remove Expired Entry" : "Revoke Link"}
                      aria-label={
                        (ended
                          ? "Remove Expired Link for "
                          : "Revoke Link for ") + name
                      }
                      onClick={() =>
                        ended
                          ? void revoke(share)
                          : (setRevokeError(""), setConfirm(share))
                      }
                    >
                      <X size={18} />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
      <span className="sr-only" role="status">
        {copied ? "Share Link copied" : ""}
      </span>
      {confirm && (
        <Modal
          title="Revoke This Link?"
          onClose={() => {
            if (!busy) setConfirm(null);
          }}
        >
          <p>
            People with the link to <strong>{confirm.name}</strong> will lose
            access. Other links are unaffected.
          </p>
          <ErrorNotice message={revokeError} />
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={!!busy}
              onClick={() => setConfirm(null)}
            >
              Keep Sharing
            </button>
            <button
              className="danger-button"
              disabled={!!busy}
              onClick={() => void revoke(confirm)}
            >
              {busy ? <Spinner /> : <X size={16} />} Revoke Link
            </button>
          </div>
        </Modal>
      )}
      {manualLink && (
        <Modal title="Copy Share Link" onClose={() => setManualLink("")}>
          <p>Select and copy the link below.</p>
          <label htmlFor="existing-share-url">Share Link</label>
          <input
            id="existing-share-url"
            readOnly
            value={manualLink}
            onFocus={(event) => event.target.select()}
          />
          <div className="dialog-actions">
            <button
              className="primary-button"
              onClick={() => setManualLink("")}
            >
              Done
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
