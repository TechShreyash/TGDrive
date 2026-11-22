import {
    type MouseEvent,
    useEffect,
    useLayoutEffect,
    useRef,
    useState,
} from "react";
import { ChevronRight, Download, RefreshCw } from "lucide-react";
import { api, ApiError, request } from "../../shared/api";
import { ErrorNotice, FileIcon, Spinner } from "../../shared/components";
import {
    fileUrl,
    formatBytes,
    itemPath,
    type DriveItem,
    type NodePage,
} from "../../shared/types";
import Preview from "../previews/Preview";
import SharedUnlock, {
    needsSharePassword,
    shareLinkUnavailable,
    SHARE_LOAD_FAILED_MESSAGE,
    SHARE_UNAVAILABLE_MESSAGE,
} from "./SharedUnlock";
import { useShareOpen } from "./useShareOpen";
import "./EmbedView.css";

export async function loadEmbeddedShare(
    path: string,
    auth: string,
    signal: AbortSignal,
) {
    if (!path || !auth)
        throw new ApiError(
            "This embed link is incomplete. Ask the owner for a new link.",
            404,
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
    // Attachment navigation keeps framed requests in the publisher’s cookie partition.
    const inFrame = window.self !== window.top;
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
    const [error, setError] = useState<"unavailable" | "unreachable" | null>(
        null,
    );
    const unavailable = error === "unavailable";
    const [locked, setLocked] = useState(false);
    const [needsTopLevel, setNeedsTopLevel] = useState(false);
    const checkingUnlock = useRef(false);
    const [attempt, setAttempt] = useState(0);
    useShareOpen(
        !loading &&
            !locked &&
            !error &&
            current.path === root &&
            Boolean(listing || preview),
        auth,
        root,
    );
    const activeLoad = useRef<AbortController | null>(null);
    const viewport = useRef<HTMLElement>(null);
    const currentCrumb = useRef<HTMLButtonElement>(null);
    const focusAfterNavigation = useRef(false);
    const focusAfterMore = useRef<{
        button: HTMLButtonElement;
        itemId: string;
    } | null>(null);
    const navigate = (nextTrail: typeof trail) => {
        activeLoad.current?.abort();
        focusAfterNavigation.current = true;
        setLoading(true);
        setTrail(nextTrail);
    };
    useEffect(() => {
        const controller = new AbortController();
        activeLoad.current = controller;
        setLoading(true);
        setError(null);
        setListing(null);
        setMore("idle");
        setPreview(null);
        void loadEmbeddedShare(current.path, auth, controller.signal)
            .then((result) => {
                if (controller.signal.aborted) return;
                checkingUnlock.current = false;
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
                if (!controller.signal.aborted) {
                    if (needsSharePassword(cause)) {
                        setNeedsTopLevel(
                            checkingUnlock.current && window.parent !== window,
                        );
                        setLocked(true);
                    } else if (shareLinkUnavailable(cause)) {
                        document.title = "Shared Item Unavailable · TGDrive";
                        setError("unavailable");
                    } else {
                        setError("unreachable");
                    }
                    checkingUnlock.current = false;
                }
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [current.path, auth, attempt]);
    useEffect(() => {
        if (loading || !focusAfterNavigation.current) return;
        focusAfterNavigation.current = false;
        viewport.current?.scrollTo(0, 0);
        currentCrumb.current?.focus({ preventScroll: true });
    }, [loading]);
    useLayoutEffect(() => {
        const pending = focusAfterMore.current;
        focusAfterMore.current = null;
        if (!pending) return;

        const focused = document.activeElement;
        // On the last page, removing Show more leaves focus on the body.
        if (
            focused === pending.button ||
            (!pending.button.isConnected && focused === document.body)
        )
            viewport.current
                ?.querySelector<HTMLButtonElement>(
                    `[data-item-id="${CSS.escape(pending.itemId)}"] .embed-item`,
                )
                ?.focus();
    }, [listing]);
    const items = listing?.items || [];
    const showMore = async (event: MouseEvent<HTMLButtonElement>) => {
        const controller = activeLoad.current;
        if (
            !listing?.next_cursor ||
            !controller ||
            controller.signal.aborted ||
            more === "loading"
        )
            return;
        const cursor = listing.next_cursor;
        const button = event.currentTarget;
        const keyboard = event.detail === 0;
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
            const first =
                next.items[0] ??
                (next.next_cursor ? undefined : listing.items.at(-1));
            if (first && keyboard && document.activeElement === button)
                focusAfterMore.current = { button, itemId: first.id };
            setListing((previous) =>
                previous?.next_cursor === cursor
                    ? {
                          ...next,
                          items: [...previous.items, ...next.items],
                      }
                    : previous,
            );
            setMore("idle");
        } catch (cause) {
            if (
                !controller.signal.aborted &&
                activeLoad.current === controller
            ) {
                if (needsSharePassword(cause)) {
                    setLocked(true);
                } else if (shareLinkUnavailable(cause)) {
                    setListing(null);
                    setPreview(null);
                    document.title = "Shared Item Unavailable · TGDrive";
                    setError("unavailable");
                    setMore("idle");
                } else {
                    setMore("error");
                }
            }
        }
    };
    if (locked)
        return (
            <SharedUnlock
                auth={auth}
                embed
                needsTopLevel={needsTopLevel}
                onUnlocked={() => {
                    checkingUnlock.current = true;
                    setLocked(false);
                    setAttempt(attempt + 1);
                }}
            />
        );
    return (
        <main
            ref={viewport}
            className="embed-view"
            id="main-content"
            tabIndex={-1}
        >
            {folder && !unavailable && (
                <header className="embed-header">
                    <nav aria-label="Shared folder path">
                        <ol>
                            {trail.map((crumb, index) => (
                                <li
                                    key={crumb.path}
                                    data-compact-hidden={
                                        index > 0 && index < trail.length - 2
                                            ? true
                                            : undefined
                                    }
                                >
                                    {index > 0 && (
                                        <ChevronRight
                                            size={16}
                                            aria-hidden="true"
                                        />
                                    )}
                                    <button
                                        ref={
                                            crumb === current
                                                ? currentCrumb
                                                : undefined
                                        }
                                        type="button"
                                        title={crumb.name}
                                        aria-current={
                                            crumb === current
                                                ? "page"
                                                : undefined
                                        }
                                        onClick={() => {
                                            if (crumb !== current)
                                                navigate(
                                                    trail.slice(0, index + 1),
                                                );
                                        }}
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
                    <ErrorNotice
                        message={
                            unavailable
                                ? SHARE_UNAVAILABLE_MESSAGE
                                : SHARE_LOAD_FAILED_MESSAGE
                        }
                    />
                    {!unavailable && (
                        <button
                            className="secondary-button"
                            onClick={() => setAttempt(attempt + 1)}
                        >
                            <RefreshCw size={16} aria-hidden="true" /> Try Again
                        </button>
                    )}
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
                                        ? navigate([
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
                                    download={inFrame ? undefined : item.name}
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
                                aria-disabled={more === "loading"}
                                aria-busy={more === "loading"}
                                onClick={(event) => void showMore(event)}
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
                    inFrame={inFrame}
                    onClose={folder ? () => setPreview(null) : undefined}
                />
            )}
        </main>
    );
}
