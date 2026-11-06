import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, RefreshCw } from "lucide-react";
import { api, errorMessage, request } from "../../shared/api";
import { ErrorNotice, FileIcon, Spinner } from "../../shared/components";
import {
    fileUrl,
    formatBytes,
    itemPath,
    type DriveItem,
    type NodePage,
} from "../../shared/types";
import Preview from "../previews/Preview";
import "./EmbedView.css";

export async function loadEmbeddedShare(
    path: string,
    auth: string,
    signal: AbortSignal,
) {
    if (!path || !auth)
        throw new Error(
            "This embed link is incomplete. Ask the owner for a new link.",
        );
    if (path.startsWith("/share_")) {
        const id = path.slice(7).split("/").filter(Boolean).at(-1) || "";
        const page = await api.sharedNodes(
            { parent_id: "/" + id, limit: 200 },
            auth,
            signal,
        );
        return { page };
    }
    const file = await request<DriveItem>(
        "/api/v1/embed?" + new URLSearchParams({ path, auth }),
        undefined,
        { signal },
    );
    return { file };
}

export default function EmbedView() {
    const [params] = useState(
        () => new URLSearchParams(window.location.search),
    );
    const auth = params.get("auth") || "";
    const root = params.get("path") || "";
    const folder = root.startsWith("/share_");
    const [trail, setTrail] = useState([{ path: root, name: "Shared Folder" }]);
    const current = trail[trail.length - 1];
    const [listing, setListing] = useState<NodePage | null>(null);
    const [more, setMore] = useState<"idle" | "loading" | "error">("idle");
    const [preview, setPreview] = useState<DriveItem | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [attempt, setAttempt] = useState(0);
    const activeLoad = useRef<AbortController | null>(null);
    useEffect(() => {
        const controller = new AbortController();
        activeLoad.current = controller;
        setLoading(true);
        setError("");
        setListing(null);
        setMore("idle");
        setPreview(null);
        void loadEmbeddedShare(current.path, auth, controller.signal)
            .then((result) => {
                if (controller.signal.aborted) return;
                if (result.file) {
                    setPreview(result.file);
                    document.title = result.file.name + " · TGDrive";
                } else {
                    const data = result.page.folder || { name: "" };
                    setListing(result.page);
                    setTrail((previous) =>
                        previous.map((crumb) =>
                            crumb.path === current.path
                                ? { ...crumb, name: data.name || crumb.name }
                                : crumb,
                        ),
                    );
                    document.title =
                        (data.name || "Shared Folder") + " · TGDrive";
                }
            })
            .catch((cause) => {
                if (!controller.signal.aborted) setError(errorMessage(cause));
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [current.path, auth, attempt]);
    const items = listing?.items || [];
    const showMore = async () => {
        const controller = activeLoad.current;
        if (
            !listing?.next_cursor ||
            !controller ||
            controller.signal.aborted ||
            more === "loading"
        )
            return;
        const cursor = listing.next_cursor;
        setMore("loading");
        try {
            const id = current.path.slice(7).split("/").filter(Boolean).at(-1);
            const next = await api.sharedNodes(
                {
                    parent_id: "/" + id,
                    limit: 500,
                    cursor,
                },
                auth,
                controller.signal,
            );
            if (controller.signal.aborted || activeLoad.current !== controller)
                return;
            setListing((previous) =>
                previous?.next_cursor === cursor
                    ? {
                          ...next,
                          items: [...previous.items, ...next.items],
                      }
                    : previous,
            );
            setMore("idle");
        } catch {
            if (!controller.signal.aborted && activeLoad.current === controller)
                setMore("error");
        }
    };
    return (
        <main className="embed-view" id="main-content" tabIndex={-1}>
            {folder && (
                <header className="embed-header">
                    <nav aria-label="Shared folder path">
                        <ol>
                            {trail.map((crumb, index) => (
                                <li key={crumb.path}>
                                    {index > 0 && (
                                        <ChevronRight
                                            size={16}
                                            aria-hidden="true"
                                        />
                                    )}
                                    <button
                                        type="button"
                                        aria-current={
                                            index === trail.length - 1
                                                ? "page"
                                                : undefined
                                        }
                                        disabled={loading}
                                        onClick={() =>
                                            setTrail(trail.slice(0, index + 1))
                                        }
                                    >
                                        {crumb.name}
                                    </button>
                                </li>
                            ))}
                        </ol>
                    </nav>
                    <button
                        type="button"
                        className="icon-button"
                        aria-label="Refresh shared folder"
                        title="Refresh shared folder"
                        disabled={loading}
                        onClick={() => setAttempt(attempt + 1)}
                    >
                        <RefreshCw size={18} aria-hidden="true" />
                    </button>
                </header>
            )}
            {loading ? (
                <p className="embed-message" role="status">
                    <Spinner /> Loading shared {folder ? "folder" : "file"}…
                </p>
            ) : error ? (
                <div className="embed-message">
                    <h1>Shared Item Unavailable</h1>
                    <ErrorNotice message={error} />
                    <p>
                        The link may have expired, been revoked, or the item may
                        be unavailable.
                    </p>
                    <button
                        className="secondary-button"
                        onClick={() => setAttempt(attempt + 1)}
                    >
                        <RefreshCw size={16} aria-hidden="true" /> Try Again
                    </button>
                </div>
            ) : folder && !items.length ? (
                <p className="embed-message">This folder is empty.</p>
            ) : folder ? (
                <ul className="embed-items" aria-label="Shared folder contents">
                    {items.map((item) => (
                        <li key={item.id} data-item-id={item.id}>
                            <button
                                type="button"
                                className="embed-item item-name-button"
                                onClick={() =>
                                    item.type === "folder"
                                        ? setTrail([
                                              ...trail,
                                              {
                                                  path: "/share_" + item.id,
                                                  name: item.name,
                                              },
                                          ])
                                        : setPreview(item)
                                }
                            >
                                <FileIcon
                                    name={item.name}
                                    folder={item.type === "folder"}
                                />
                                <span>{item.name}</span>
                                <span className="embed-item-size">
                                    {item.type === "folder"
                                        ? "Folder"
                                        : formatBytes(item.size)}
                                </span>
                            </button>
                            {item.type === "file" && (
                                <a
                                    className="icon-button"
                                    href={fileUrl(itemPath(item), auth, true)}
                                    download={item.name}
                                    aria-label={"Download " + item.name}
                                    title={"Download " + item.name}
                                >
                                    <Download size={18} aria-hidden="true" />
                                </a>
                            )}
                        </li>
                    ))}
                    {listing?.next_cursor && (
                        <li className="embed-more">
                            <button
                                type="button"
                                className="secondary-button"
                                disabled={more === "loading"}
                                onClick={() => void showMore()}
                            >
                                {more === "loading" && <Spinner />}
                                {more === "error"
                                    ? "Could not load more. Try Again"
                                    : "Show more"}
                            </button>
                            <span className="embed-item-size">
                                {items.length} of {listing.total}
                            </span>
                        </li>
                    )}
                </ul>
            ) : null}
            {preview && (
                <Preview
                    key={preview.id}
                    item={preview}
                    auth={auth}
                    onClose={folder ? () => setPreview(null) : undefined}
                />
            )}
        </main>
    );
}
