import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
    Star,
    AlertCircle,
    Folder,
    Upload,
    Move,
    Trash2,
    RefreshCw,
    Download,
    Server,
    ChevronRight,
    ChevronLeft,
    MoreHorizontal,
    Pencil,
    Eye,
    Link,
    List,
    LayoutGrid,
    ArrowUp,
    ArrowDown,
    SquareCheck,
    Archive,
    FolderArchive,
} from "lucide-react";
import type { ActionDialogState } from "./ActionDialog";
import {
    itemPath,
    fileUrl,
    formatBytes,
    formatDate,
    type DriveItem,
    type Directory,
    type Share,
} from "../../shared/types";
import { api } from "../../shared/api";
import { shareExpired } from "../shares/SharesView";
import { FileIcon, Spinner } from "../../shared/components";
import SortMenu, { type FileSort } from "./SortMenu";
import "./FileBrowser.css";
import FileThumbnail from "./FileThumbnail";
import { isExtractableArchive } from "./archiveApi";
import { COMPACT_LAYOUT_QUERY } from "../../shared/responsive";
import {
    defaultSortOrder,
    sortDriveItems,
    type SortOrder,
} from "./fileSorting";

function positionItemMenu(details: HTMLDetailsElement) {
    const panel = details.querySelector<HTMLElement>(".action-menu");
    const trigger = details.querySelector("summary");
    if (!panel || !trigger || !panel.showPopover) return;
    if (!details.open) {
        if (panel.matches(":popover-open")) panel.hidePopover();
        return;
    }
    panel.style.maxHeight = Math.max(96, window.innerHeight - 24) + "px";
    panel.showPopover();
    const bounds = trigger.getBoundingClientRect();
    const menu = panel.getBoundingClientRect();
    const roomBelow = window.innerHeight - bounds.bottom - 18;
    panel.style.left =
        Math.max(
            12,
            Math.min(
                bounds.right - menu.width,
                window.innerWidth - menu.width - 12,
            ),
        ) + "px";
    panel.style.top =
        Math.max(
            12,
            roomBelow >= menu.height
                ? bounds.bottom + 6
                : bounds.top - menu.height - 6,
        ) + "px";
}

function hideItemMenus() {
    document
        .querySelectorAll<HTMLDetailsElement>(".item-actions[open]")
        .forEach((menu) => {
            menu.open = false;
            positionItemMenu(menu);
        });
}
export default function FileBrowser({
    directory,
    listingKey,
    shareRefreshKey,
    highlightId = "",
    loading,
    refreshing = false,
    error,
    isPublic,
    isTrash,
    isStarred = false,
    onStar,
    isSearch,
    auth,
    onReload,
    onUpload,
    onOpen,
    onDialog,
    onAction,
    searchPagination,
    searchSort,
    onSearchSort,
    searchOrder,
    onSearchOrder,
    searchItems,
    header,
    searchQuery = "",
}: {
    directory: Directory;
    listingKey: string;
    shareRefreshKey: string;
    highlightId?: string;
    loading: boolean;
    refreshing?: boolean;
    error: string;
    isPublic: boolean;
    isTrash: boolean;
    isStarred?: boolean;
    onStar?: (item: DriveItem) => void;
    isSearch: boolean;
    auth: string;
    onReload: () => void;
    onUpload: () => void;
    onOpen: (item: DriveItem) => void;
    onDialog: (dialog: ActionDialogState) => void;
    onAction: (action: "trash" | "restore", items: DriveItem[]) => void;
    searchPagination?: {
        offset: number;
        hasMore: boolean;
        onPrevious: () => void;
        onNext: () => void;
    };
    searchSort?: FileSort;
    onSearchSort?: (value: FileSort) => void;
    searchOrder?: SortOrder;
    onSearchOrder?: (value: SortOrder) => void;
    searchItems?: DriveItem[];
    header?: ReactNode;
    searchQuery?: string;
}) {
    const [activeShares, setActiveShares] = useState<Share[]>([]);
    useEffect(() => {
        let ignore = false;
        if (isPublic || isTrash) {
            setActiveShares([]);
            return;
        }
        void api
            .shares()
            .then((result) => {
                if (!ignore) setActiveShares(result.shares);
            })
            .catch(() => {
                if (!ignore) setActiveShares([]);
            });
        return () => {
            ignore = true;
        };
    }, [shareRefreshKey, isPublic, isTrash]);
    const [shareTick, tickShares] = useState(0);
    useEffect(() => {
        if (!activeShares.length) return;
        const timer = setInterval(() => tickShares((n) => n + 1), 30000);
        return () => clearInterval(timer);
    }, [activeShares]);
    const shareIndex = useMemo(() => {
        const active = activeShares.filter((share) => !shareExpired(share));
        return {
            ids: new Set(active.map((share) => share.node_id)),
            paths: new Set(
                active.map((share) => share.path.replace(/\/+$/, "")),
            ),
        };
    }, [activeShares, shareTick]);
    const sharing = (item: DriveItem) => {
        let path = itemPath(item);
        if (shareIndex.ids.has(item.id) || shareIndex.paths.has(path))
            return "Shared by Link";
        while (path.includes("/")) {
            path = path.slice(0, path.lastIndexOf("/"));
            if (shareIndex.paths.has(path))
                return "Shared Through Parent Folder";
        }
        return "";
    };
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [selectionMode, setSelectionMode] = useState(false);
    const pointerKind = useRef("");
    const selectionActive = useRef(false);
    selectionActive.current = selectionMode || selected.size > 0;
    const [localSort, setLocalSort] = useState<FileSort>("name");
    const [localOrder, setLocalOrder] = useState<SortOrder>("asc");
    const [layout, setLayout] = useState<"list" | "grid">(() => {
        try {
            return localStorage.getItem("tgdrive.file-view") === "grid"
                ? "grid"
                : "list";
        } catch {
            return "list";
        }
    });
    const sort = searchSort || localSort;
    const order = searchOrder || localOrder;
    const setOrder = (value: SortOrder) => {
        if (searchPagination) onSearchOrder?.(value);
        else setLocalOrder(value);
        setPage(0);
    };
    const setSort = (value: FileSort, toggle = false) => {
        const direction =
            toggle && value === sort
                ? order === "asc"
                    ? "desc"
                    : "asc"
                : value === sort
                  ? order
                  : defaultSortOrder(value);
        if (searchPagination) {
            onSearchSort?.(value);
            onSearchOrder?.(direction);
        } else {
            setLocalSort(value);
            setLocalOrder(direction);
        }
        setPage(0);
    };
    const setView = (value: "list" | "grid") => {
        setLayout(value);
        try {
            localStorage.setItem("tgdrive.file-view", value);
        } catch {}
        hideItemMenus();
    };
    const [page, setPage] = useState(0);
    useEffect(() => {
        setSelected(new Set());
        setSelectionMode(false);
        setPage(0);
    }, [listingKey]);
    useEffect(() => {
        const escape = (event: KeyboardEvent) => {
            if (
                event.key !== "Escape" ||
                document.querySelector("dialog[open]") ||
                (event.target as HTMLElement).closest(
                    "input:not([type=checkbox]),textarea,[role=menu]",
                )
            )
                return;
            const openMenu = document.querySelector<HTMLDetailsElement>(
                ".item-actions[open]",
            );
            if (openMenu) {
                event.preventDefault();
                hideItemMenus();
                openMenu.querySelector("summary")?.focus();
                return;
            }
            if (!selectionActive.current) return;
            event.preventDefault();
            const row =
                (event.target as HTMLElement).closest<HTMLElement>(
                    ".file-row",
                ) || document.querySelector<HTMLElement>(".file-row.selected");
            const item =
                row?.querySelector<HTMLButtonElement>(".item-name-button");
            setSelected(new Set());
            setSelectionMode(false);
            // Selection checkboxes disappear on Escape. Return keyboard focus to a
            // surviving control instead of letting the browser focus the whole scroller.
            requestAnimationFrame(() => {
                const target = item?.isConnected
                    ? item
                    : document.querySelector<HTMLButtonElement>(
                          ".file-selection-toggle",
                      );
                target?.focus({ preventScroll: true });
            });
        };
        document.addEventListener("keydown", escape);
        return () => document.removeEventListener("keydown", escape);
    }, []);
    const allItems = useMemo(() => {
        const items = (
            searchItems || Object.values(directory.contents || {})
        ).slice();
        return searchPagination ? items : sortDriveItems(items, sort, order);
    }, [directory.contents, searchItems, searchPagination, sort, order]);
    useEffect(() => {
        // Retain browsing state on refresh, dropping only selections that disappeared.
        const ids = new Set(allItems.map((item) => item.id));
        setSelected((current) => {
            const next = new Set([...current].filter((id) => ids.has(id)));
            return next.size === current.size ? current : next;
        });
        setPage((current) =>
            Math.min(
                current,
                Math.max(0, Math.ceil(allItems.length / 100) - 1),
            ),
        );
    }, [allItems]);
    useEffect(() => {
        const list = document.querySelector<HTMLElement>(".file-list-scroll");
        const scroller = window.matchMedia(COMPACT_LAYOUT_QUERY).matches
            ? list?.closest(".workspace")
            : list;
        scroller?.scrollTo({ top: 0 });
    }, [page, listingKey, layout]);
    const highlighted = useRef("");
    useEffect(() => {
        if (loading || !highlightId) return;
        const target = JSON.stringify([
            listingKey,
            highlightId,
            sort,
            order,
            layout,
        ]);
        if (highlighted.current === target) return;
        const index = allItems.findIndex((item) => item.id === highlightId);
        if (index < 0) return;
        setPage(Math.floor(index / 100));
        setSelected(new Set([highlightId]));
        setSelectionMode(false);
        let frame = requestAnimationFrame(() => {
            frame = requestAnimationFrame(() => {
                highlighted.current = target;
                const row = document.querySelector<HTMLElement>(
                    '[data-item-id="' + CSS.escape(highlightId) + '"]',
                );
                row?.scrollIntoView({ block: "center" });
                row?.querySelector<HTMLButtonElement>(
                    ".item-name-button",
                )?.focus({ preventScroll: true });
            });
        });
        return () => cancelAnimationFrame(frame);
    }, [allItems, listingKey, highlightId, loading, sort, order, layout]);
    const visibleItems = allItems.slice(page * 100, (page + 1) * 100);
    const chosen = allItems.filter((item) => selected.has(item.id));
    const closeItemMenus = () => {
        const trigger = document.activeElement
            ?.closest(".item-actions")
            ?.querySelector("summary");
        hideItemMenus();
        trigger?.focus();
    };
    const showDialog = (dialog: ActionDialogState) => {
        closeItemMenus();
        onDialog(dialog);
    };
    const openPrimary = (
        item: DriveItem,
        keyboard = false,
        doubleClick = false,
    ) => {
        // Opening never enters selection mode. Explicit selection has the same
        // behavior for touch, mouse and keyboard, without double-tap timing.
        if (!isPublic && selectionMode) {
            setSelected((current) => {
                const next = new Set(current);
                if (next.has(item.id)) next.delete(item.id);
                else next.add(item.id);
                return next;
            });
        } else if (
            !isTrash &&
            (pointerKind.current !== "mouse" || keyboard || doubleClick)
        ) {
            setSelected(new Set());
            onOpen(item);
        }
    };
    useEffect(() => {
        const close = (event: MouseEvent) => {
            if (!(event.target as Element).closest(".item-actions"))
                hideItemMenus();
        };
        const reposition = (event: Event) => {
            if ((event.target as Element)?.closest?.(".action-menu")) return;
            document
                .querySelectorAll<HTMLDetailsElement>(".item-actions[open]")
                .forEach((details) => {
                    const bounds = details
                        .querySelector("summary")
                        ?.getBoundingClientRect();
                    if (
                        !bounds ||
                        bounds.bottom < 0 ||
                        bounds.top > window.innerHeight
                    ) {
                        details.open = false;
                    }
                    positionItemMenu(details);
                });
        };
        document.addEventListener("click", close);
        window.addEventListener("resize", hideItemMenus);
        window.addEventListener("scroll", reposition, true);
        return () => {
            document.removeEventListener("click", close);
            window.removeEventListener("resize", hideItemMenus);
            window.removeEventListener("scroll", reposition, true);
            hideItemMenus();
        };
    }, []);
    const toggle = (id: string) =>
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    const Collection = layout === "grid" ? "div" : "table";
    const CollectionBody = layout === "grid" ? "ul" : "tbody";
    const Row = layout === "grid" ? "li" : "tr";
    const Cell = layout === "grid" ? "div" : "td";
    return (
        <div
            className="file-browser"
            onPointerDownCapture={(event) => {
                pointerKind.current = event.pointerType;
            }}
        >
            <div
                className="file-toolbar"
                data-selection={selectionMode && !isPublic ? "true" : undefined}
            >
                {header}
                <div className="file-toolbar-controls">
                    {selectionMode && !isPublic ? (
                        <>
                            {layout === "grid" && (
                                <label className="grid-select-all file-checkbox-target">
                                    <input
                                        type="checkbox"
                                        aria-label="Select All Files on This Page"
                                        checked={
                                            visibleItems.length > 0 &&
                                            visibleItems.every((item) =>
                                                selected.has(item.id),
                                            )
                                        }
                                        ref={(input) => {
                                            if (input) {
                                                const count =
                                                    visibleItems.filter(
                                                        (item) =>
                                                            selected.has(
                                                                item.id,
                                                            ),
                                                    ).length;
                                                input.indeterminate =
                                                    count > 0 &&
                                                    count < visibleItems.length;
                                            }
                                        }}
                                        onChange={(event) => {
                                            const next = new Set(selected);
                                            for (const item of visibleItems)
                                                event.target.checked
                                                    ? next.add(item.id)
                                                    : next.delete(item.id);
                                            setSelected(next);
                                        }}
                                    />
                                </label>
                            )}
                            <span className="selection-count" role="status">
                                {chosen.length} selected
                            </span>
                            <button
                                className="text-button"
                                onClick={() => {
                                    setSelected(new Set());
                                    setSelectionMode(false);
                                    requestAnimationFrame(() => {
                                        document
                                            .querySelector<HTMLButtonElement>(
                                                ".file-selection-toggle",
                                            )
                                            ?.focus({ preventScroll: true });
                                    });
                                }}
                            >
                                Done
                            </button>
                            <div className="toolbar-divider" />
                            {isTrash ? (
                                <>
                                    <button
                                        className="text-button file-bulk-action"
                                        disabled={!chosen.length}
                                        aria-label="Restore"
                                        title="Restore"
                                        onClick={() =>
                                            onAction("restore", chosen)
                                        }
                                    >
                                        <RefreshCw size={16} />{" "}
                                        <span>Restore</span>
                                    </button>
                                    <button
                                        className="text-button danger-text file-bulk-action"
                                        disabled={!chosen.length}
                                        aria-label="Delete Permanently"
                                        title="Delete Permanently"
                                        onClick={() =>
                                            showDialog({
                                                type: "delete",
                                                items: chosen,
                                            })
                                        }
                                    >
                                        <Trash2 size={16} />{" "}
                                        <span>Delete Permanently</span>
                                    </button>
                                </>
                            ) : (
                                <>
                                    <button
                                        className="text-button file-bulk-action"
                                        disabled={
                                            !chosen.length ||
                                            directory.read_only ||
                                            chosen.some(
                                                (item) => item.read_only,
                                            )
                                        }
                                        aria-label="Create ZIP"
                                        title="Create ZIP"
                                        onClick={() =>
                                            showDialog({
                                                type: "archive",
                                                items: chosen,
                                            })
                                        }
                                    >
                                        <Archive size={16} aria-hidden="true" />{" "}
                                        <span>Create ZIP</span>
                                    </button>
                                    <button
                                        className="text-button file-bulk-action"
                                        disabled={!chosen.length}
                                        aria-label="Move"
                                        title="Move"
                                        onClick={() =>
                                            showDialog({
                                                type: "move",
                                                items: chosen,
                                            })
                                        }
                                    >
                                        <Move size={16} /> <span>Move</span>
                                    </button>
                                    <button
                                        className="text-button file-bulk-action"
                                        disabled={!chosen.length}
                                        aria-label="Trash"
                                        title="Trash"
                                        onClick={() =>
                                            onAction("trash", chosen)
                                        }
                                    >
                                        <Trash2 size={16} /> <span>Trash</span>
                                    </button>
                                </>
                            )}
                        </>
                    ) : (
                        <>
                            <span className="file-item-count">
                                {searchPagination
                                    ? allItems.length
                                        ? "Showing " +
                                          (searchPagination.offset + 1) +
                                          "–" +
                                          (searchPagination.offset +
                                              allItems.length) +
                                          (searchPagination.hasMore
                                              ? " · More available"
                                              : "")
                                        : "No Results"
                                    : allItems.length +
                                      (allItems.length === 1
                                          ? " item"
                                          : " items")}
                                {isPublic ? " · Read-only" : ""}
                            </span>
                            <div className="file-toolbar-actions">
                                {!isPublic && (
                                    <button
                                        type="button"
                                        className="text-button file-selection-toggle"
                                        title="Select files and folders"
                                        aria-pressed={selectionMode}
                                        onClick={() => {
                                            setSelectionMode(!selectionMode);
                                            setSelected(new Set());
                                        }}
                                    >
                                        <SquareCheck
                                            size={18}
                                            aria-hidden="true"
                                        />
                                        <span>
                                            {selectionMode ? "Done" : "Select"}
                                        </span>
                                    </button>
                                )}
                                <SortMenu
                                    value={sort}
                                    order={order}
                                    onChange={(value) => setSort(value)}
                                    onOrderChange={setOrder}
                                    view={layout}
                                    onViewChange={setView}
                                />
                            </div>
                        </>
                    )}
                    <div
                        className="file-view-toggle"
                        role="group"
                        aria-label="File view"
                    >
                        <button
                            type="button"
                            className="icon-button"
                            aria-label="List view"
                            title="List view"
                            aria-pressed={layout === "list"}
                            onClick={() => setView("list")}
                        >
                            <List size={18} />
                        </button>
                        <button
                            type="button"
                            className="icon-button"
                            aria-label="Grid view"
                            title="Grid view"
                            aria-pressed={layout === "grid"}
                            onClick={() => setView("grid")}
                        >
                            <LayoutGrid size={18} />
                        </button>
                    </div>
                </div>
            </div>
            {!isPublic && directory.read_only && (
                <p
                    className="settings-help demo-files-read-only-note"
                    role="note"
                >
                    Demo Files is read-only. You can open, preview and download
                    these files. Use My Files to upload files, create folders
                    and try changes.
                </p>
            )}
            <div
                className={
                    "file-list-scroll" +
                    (layout === "grid" ? " file-grid-scroll" : "")
                }
                tabIndex={0}
                aria-label="File list"
            >
                {error ? (
                    <div className="empty-state">
                        <AlertCircle size={38} />
                        <h2>
                            {isPublic
                                ? "Shared Folder Unavailable"
                                : "Could Not Load Your Files"}
                        </h2>
                        <p role="alert">{error}</p>
                        <button
                            className="secondary-button"
                            disabled={loading || refreshing}
                            onClick={onReload}
                        >
                            {refreshing && <Spinner />}
                            {refreshing ? "Refreshing…" : "Try Again"}
                        </button>
                    </div>
                ) : loading ? (
                    <div className="loading-state" role="status">
                        <Spinner /> Loading files…
                    </div>
                ) : allItems.length === 0 ? (
                    <div className="empty-state">
                        <Folder size={46} />
                        <h2>
                            {isStarred
                                ? searchQuery
                                    ? "No Matching Starred Items"
                                    : "No Starred Items Yet"
                                : isTrash && searchQuery
                                  ? "No Matching Trashed Items"
                                  : isTrash
                                    ? "Trash Is Empty"
                                    : isSearch
                                      ? "No Matching Files or Folders"
                                      : "Nothing Here Yet"}
                        </h2>
                        <p>
                            {isStarred
                                ? "Star a file or folder from its actions menu to find it here."
                                : isTrash && searchQuery
                                  ? "Try another name or clear the search. This search stays in Trash."
                                  : isTrash
                                    ? "Items you trash stay here until you restore or permanently delete them."
                                    : isSearch
                                      ? "Try another file or folder name."
                                      : isPublic
                                        ? "The owner has not added any files to this folder."
                                        : "Upload your first file or create a folder to get started."}
                        </p>
                        {!isPublic && !isTrash && !isSearch && !isStarred && (
                            <button
                                className="primary-button"
                                onClick={onUpload}
                            >
                                <Upload size={17} /> Upload Files
                            </button>
                        )}
                    </div>
                ) : (
                    <>
                        <div className="file-table-wrap">
                            <Collection
                                className={
                                    "file-table" +
                                    (layout === "grid" ? " file-grid" : "")
                                }
                            >
                                {layout === "list" && (
                                    <thead>
                                        <tr>
                                            <th
                                                scope="col"
                                                aria-sort={
                                                    sort === "name"
                                                        ? order === "asc"
                                                            ? "ascending"
                                                            : "descending"
                                                        : undefined
                                                }
                                            >
                                                <div className="name-header-content">
                                                    {!isPublic &&
                                                        selectionMode && (
                                                            <span className="file-selection-slot">
                                                                {selectionMode && (
                                                                    <label className="file-checkbox-target">
                                                                        <input
                                                                            type="checkbox"
                                                                            aria-label="Select All Files on This Page"
                                                                            ref={(
                                                                                input,
                                                                            ) => {
                                                                                if (
                                                                                    input
                                                                                ) {
                                                                                    const count =
                                                                                        visibleItems.filter(
                                                                                            (
                                                                                                item,
                                                                                            ) =>
                                                                                                selected.has(
                                                                                                    item.id,
                                                                                                ),
                                                                                        ).length;
                                                                                    input.indeterminate =
                                                                                        count >
                                                                                            0 &&
                                                                                        count <
                                                                                            visibleItems.length;
                                                                                }
                                                                            }}
                                                                            checked={
                                                                                visibleItems.length >
                                                                                    0 &&
                                                                                visibleItems.every(
                                                                                    (
                                                                                        item,
                                                                                    ) =>
                                                                                        selected.has(
                                                                                            item.id,
                                                                                        ),
                                                                                )
                                                                            }
                                                                            onChange={(
                                                                                event,
                                                                            ) => {
                                                                                const next =
                                                                                    new Set(
                                                                                        selected,
                                                                                    );
                                                                                for (const item of visibleItems)
                                                                                    event
                                                                                        .target
                                                                                        .checked
                                                                                        ? next.add(
                                                                                              item.id,
                                                                                          )
                                                                                        : next.delete(
                                                                                              item.id,
                                                                                          );
                                                                                setSelected(
                                                                                    next,
                                                                                );
                                                                            }}
                                                                        />
                                                                    </label>
                                                                )}
                                                            </span>
                                                        )}
                                                    <span
                                                        className="file-icon-spacer"
                                                        aria-hidden="true"
                                                    />
                                                    <button
                                                        type="button"
                                                        className="column-sort-button"
                                                        aria-label="Sort by Name"
                                                        onClick={() => {
                                                            setSort(
                                                                "name",
                                                                true,
                                                            );
                                                            setPage(0);
                                                        }}
                                                    >
                                                        Name{" "}
                                                        {sort === "name" &&
                                                            (order === "asc" ? (
                                                                <ArrowUp
                                                                    size={12}
                                                                    aria-hidden="true"
                                                                />
                                                            ) : (
                                                                <ArrowDown
                                                                    size={12}
                                                                    aria-hidden="true"
                                                                />
                                                            ))}
                                                    </button>
                                                </div>
                                            </th>
                                            <th
                                                scope="col"
                                                className="date-cell"
                                                aria-sort={
                                                    sort === "modified"
                                                        ? order === "asc"
                                                            ? "ascending"
                                                            : "descending"
                                                        : undefined
                                                }
                                            >
                                                <button
                                                    type="button"
                                                    className="column-sort-button"
                                                    aria-label="Sort by Date Added"
                                                    onClick={() => {
                                                        setSort(
                                                            "modified",
                                                            true,
                                                        );
                                                        setPage(0);
                                                    }}
                                                >
                                                    Added
                                                </button>
                                            </th>
                                            <th
                                                scope="col"
                                                className="size-cell"
                                                aria-sort={
                                                    sort === "size"
                                                        ? order === "asc"
                                                            ? "ascending"
                                                            : "descending"
                                                        : undefined
                                                }
                                            >
                                                <button
                                                    type="button"
                                                    className="column-sort-button"
                                                    aria-label="Sort by File Size"
                                                    onClick={() => {
                                                        setSort("size", true);
                                                        setPage(0);
                                                    }}
                                                >
                                                    Size
                                                </button>
                                            </th>
                                            <th
                                                scope="col"
                                                className="actions-cell"
                                            >
                                                <span className="sr-only">
                                                    Actions
                                                </span>
                                            </th>
                                        </tr>
                                    </thead>
                                )}
                                <CollectionBody
                                    aria-label={
                                        layout === "grid"
                                            ? "Files and folders"
                                            : undefined
                                    }
                                >
                                    {visibleItems.map((item) => (
                                        <Row
                                            key={item.id}
                                            className={
                                                "file-row " +
                                                (selected.has(item.id)
                                                    ? "selected"
                                                    : "")
                                            }
                                            data-item-id={item.id}
                                            onClick={(event) => {
                                                if (
                                                    (event.detail > 1 &&
                                                        pointerKind.current ===
                                                            "mouse" &&
                                                        !selectionMode) ||
                                                    (
                                                        event.target as Element
                                                    ).closest(
                                                        "button,a,input,label,summary,details",
                                                    )
                                                )
                                                    return;
                                                openPrimary(item);
                                            }}
                                            onDoubleClick={(event) => {
                                                if (
                                                    selectionMode ||
                                                    pointerKind.current !==
                                                        "mouse" ||
                                                    (
                                                        event.target as Element
                                                    ).closest(
                                                        "a,input,label,summary,details,button:not(.item-name-button)",
                                                    )
                                                )
                                                    return;
                                                openPrimary(item, false, true);
                                            }}
                                        >
                                            <Cell className="name-cell">
                                                {layout === "grid" && (
                                                    <FileThumbnail
                                                        item={item}
                                                        auth={auth}
                                                        disabled={isTrash}
                                                    />
                                                )}
                                                <div className="name-cell-content">
                                                    {!isPublic &&
                                                        selectionMode && (
                                                            <span className="file-selection-slot">
                                                                {selectionMode && (
                                                                    <label className="file-checkbox-target">
                                                                        <input
                                                                            type="checkbox"
                                                                            aria-label={
                                                                                "Select " +
                                                                                item.name
                                                                            }
                                                                            checked={selected.has(
                                                                                item.id,
                                                                            )}
                                                                            onChange={() =>
                                                                                toggle(
                                                                                    item.id,
                                                                                )
                                                                            }
                                                                        />
                                                                    </label>
                                                                )}
                                                            </span>
                                                        )}
                                                    {layout === "list" && (
                                                        <FileIcon
                                                            name={item.name}
                                                            folder={
                                                                item.type ===
                                                                "folder"
                                                            }
                                                        />
                                                    )}
                                                    <div className="file-name-details">
                                                        <div className="file-title-line">
                                                            <button
                                                                className="file-name item-name-button"
                                                                title={
                                                                    item.name
                                                                }
                                                                onClick={(
                                                                    event,
                                                                ) => {
                                                                    if (
                                                                        event.detail >
                                                                            1 &&
                                                                        pointerKind.current ===
                                                                            "mouse" &&
                                                                        !selectionMode
                                                                    )
                                                                        return;
                                                                    openPrimary(
                                                                        item,
                                                                        event.detail ===
                                                                            0,
                                                                    );
                                                                }}
                                                            >
                                                                {item.name}
                                                            </button>
                                                            {!isPublic &&
                                                                ((!isStarred &&
                                                                    item.starred) ||
                                                                    (!isTrash &&
                                                                        sharing(
                                                                            item,
                                                                        )) ||
                                                                    item.read_only) && (
                                                                    <span className="file-item-status">
                                                                        {item.read_only && (
                                                                            <span
                                                                                className="file-status-label demo-files-read-only-label"
                                                                                title="Read-only in the public demo"
                                                                            >
                                                                                Read-only
                                                                            </span>
                                                                        )}
                                                                        {!isStarred &&
                                                                            item.starred && (
                                                                                <span className="file-status-label">
                                                                                    Starred
                                                                                </span>
                                                                            )}
                                                                        {!isTrash &&
                                                                            sharing(
                                                                                item,
                                                                            ) && (
                                                                                <span
                                                                                    className="file-status-label"
                                                                                    title={sharing(
                                                                                        item,
                                                                                    )}
                                                                                    aria-label={sharing(
                                                                                        item,
                                                                                    )}
                                                                                >
                                                                                    Shared
                                                                                </span>
                                                                            )}
                                                                    </span>
                                                                )}
                                                        </div>
                                                        <span className="file-size-mobile">
                                                            {item.type ===
                                                            "folder"
                                                                ? "Folder"
                                                                : formatBytes(
                                                                      item.size,
                                                                  )}
                                                        </span>
                                                        {(isSearch ||
                                                            isTrash ||
                                                            isStarred) && (
                                                            <span
                                                                className="search-result-location"
                                                                title={[
                                                                    "My Files",
                                                                    ...(item.parent_names ||
                                                                        []),
                                                                ].join(" / ")}
                                                            >
                                                                {isTrash
                                                                    ? "Deleted from: "
                                                                    : ""}
                                                                {[
                                                                    "My Files",
                                                                    ...(item.parent_names ||
                                                                        []),
                                                                ].join(" / ")}
                                                            </span>
                                                        )}
                                                    </div>
                                                    {item.type === "folder" &&
                                                        layout !== "grid" && (
                                                            <span className="sr-only">
                                                                Folder
                                                            </span>
                                                        )}
                                                </div>
                                            </Cell>
                                            <Cell className="date-cell">
                                                {formatDate(item.upload_date)}
                                            </Cell>
                                            <Cell className="size-cell">
                                                {item.type === "folder"
                                                    ? layout === "grid"
                                                        ? "Folder"
                                                        : "—"
                                                    : formatBytes(item.size)}
                                            </Cell>
                                            <Cell className="actions-cell">
                                                {isPublic ? (
                                                    <a
                                                        className="icon-button"
                                                        href={
                                                            item.type === "file"
                                                                ? fileUrl(
                                                                      itemPath(
                                                                          item,
                                                                      ),
                                                                      auth,
                                                                      true,
                                                                  )
                                                                : "/?" +
                                                                  new URLSearchParams(
                                                                      {
                                                                          path:
                                                                              "/share_" +
                                                                              itemPath(
                                                                                  item,
                                                                              ).slice(
                                                                                  1,
                                                                              ),
                                                                          auth,
                                                                      },
                                                                  )
                                                        }
                                                        download={
                                                            item.type === "file"
                                                                ? item.name
                                                                : undefined
                                                        }
                                                        aria-label={
                                                            (item.type ===
                                                            "file"
                                                                ? "Download "
                                                                : "Open ") +
                                                            item.name
                                                        }
                                                        onClick={
                                                            item.type ===
                                                            "folder"
                                                                ? (event) => {
                                                                      event.preventDefault();
                                                                      onOpen(
                                                                          item,
                                                                      );
                                                                  }
                                                                : undefined
                                                        }
                                                    >
                                                        {item.type ===
                                                        "file" ? (
                                                            <Download
                                                                size={18}
                                                            />
                                                        ) : (
                                                            <ChevronRight
                                                                size={18}
                                                            />
                                                        )}
                                                    </a>
                                                ) : (
                                                    <details
                                                        className="item-actions"
                                                        name="file-actions"
                                                        onToggle={(event) => {
                                                            if (
                                                                event.target ===
                                                                event.currentTarget
                                                            )
                                                                positionItemMenu(
                                                                    event.currentTarget,
                                                                );
                                                        }}
                                                    >
                                                        <summary
                                                            className="icon-button"
                                                            aria-label={
                                                                "Actions for " +
                                                                item.name
                                                            }
                                                        >
                                                            <MoreHorizontal
                                                                size={20}
                                                            />
                                                        </summary>
                                                        <div
                                                            className="action-menu"
                                                            popover="manual"
                                                        >
                                                            {isTrash ? (
                                                                <>
                                                                    <button
                                                                        onClick={() => {
                                                                            closeItemMenus();
                                                                            onAction(
                                                                                "restore",
                                                                                [
                                                                                    item,
                                                                                ],
                                                                            );
                                                                        }}
                                                                    >
                                                                        <RefreshCw
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Restore
                                                                    </button>
                                                                    <button
                                                                        className="danger-text"
                                                                        onClick={() =>
                                                                            showDialog(
                                                                                {
                                                                                    type: "delete",
                                                                                    items: [
                                                                                        item,
                                                                                    ],
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        <Trash2
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Delete
                                                                        Permanently
                                                                    </button>
                                                                </>
                                                            ) : (
                                                                <>
                                                                    <button
                                                                        onClick={() => {
                                                                            closeItemMenus();
                                                                            onOpen(
                                                                                item,
                                                                            );
                                                                        }}
                                                                    >
                                                                        <Eye
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Open
                                                                    </button>
                                                                    {item.type ===
                                                                        "file" && (
                                                                        <>
                                                                            <a
                                                                                href={fileUrl(
                                                                                    itemPath(
                                                                                        item,
                                                                                    ),
                                                                                    "",
                                                                                    true,
                                                                                )}
                                                                                download={
                                                                                    item.name
                                                                                }
                                                                                onClick={
                                                                                    closeItemMenus
                                                                                }
                                                                            >
                                                                                <Download
                                                                                    size={
                                                                                        16
                                                                                    }
                                                                                />{" "}
                                                                                Download
                                                                            </a>
                                                                            <button
                                                                                role="menuitem"
                                                                                onClick={() =>
                                                                                    showDialog(
                                                                                        {
                                                                                            type: "download-link",
                                                                                            item,
                                                                                        },
                                                                                    )
                                                                                }
                                                                            >
                                                                                <Link
                                                                                    size={
                                                                                        16
                                                                                    }
                                                                                />{" "}
                                                                                Copy
                                                                                Download
                                                                                Link
                                                                            </button>
                                                                            <button
                                                                                role="menuitem"
                                                                                title="Cache the whole file on the server for faster streaming and downloads"
                                                                                onClick={() =>
                                                                                    showDialog(
                                                                                        {
                                                                                            type: "prepare",
                                                                                            item,
                                                                                        },
                                                                                    )
                                                                                }
                                                                            >
                                                                                <Server
                                                                                    size={
                                                                                        16
                                                                                    }
                                                                                    aria-hidden="true"
                                                                                />{" "}
                                                                                Preload
                                                                                file
                                                                            </button>
                                                                        </>
                                                                    )}
                                                                    {onStar && (
                                                                        <button
                                                                            onClick={() => {
                                                                                closeItemMenus();
                                                                                onStar(
                                                                                    item,
                                                                                );
                                                                            }}
                                                                        >
                                                                            <Star
                                                                                size={
                                                                                    16
                                                                                }
                                                                                fill={
                                                                                    item.starred
                                                                                        ? "currentColor"
                                                                                        : "none"
                                                                                }
                                                                            />
                                                                            {item.starred
                                                                                ? "Remove from Starred"
                                                                                : "Add to Starred"}
                                                                        </button>
                                                                    )}
                                                                    {isExtractableArchive(
                                                                        item,
                                                                    ) && (
                                                                        <button
                                                                            disabled={
                                                                                item.read_only ||
                                                                                directory.read_only
                                                                            }
                                                                            onClick={() =>
                                                                                showDialog(
                                                                                    {
                                                                                        type: "extract",
                                                                                        item,
                                                                                    },
                                                                                )
                                                                            }
                                                                        >
                                                                            <FolderArchive
                                                                                size={
                                                                                    16
                                                                                }
                                                                                aria-hidden="true"
                                                                            />{" "}
                                                                            Extract
                                                                            archive
                                                                        </button>
                                                                    )}
                                                                    <button
                                                                        disabled={
                                                                            item.read_only ||
                                                                            directory.read_only
                                                                        }
                                                                        onClick={() =>
                                                                            showDialog(
                                                                                {
                                                                                    type: "archive",
                                                                                    items: [
                                                                                        item,
                                                                                    ],
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        <Archive
                                                                            size={
                                                                                16
                                                                            }
                                                                            aria-hidden="true"
                                                                        />{" "}
                                                                        Create
                                                                        ZIP
                                                                    </button>
                                                                    <button
                                                                        onClick={() =>
                                                                            showDialog(
                                                                                {
                                                                                    type: "rename",
                                                                                    item,
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        <Pencil
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Rename
                                                                    </button>
                                                                    <button
                                                                        onClick={() =>
                                                                            showDialog(
                                                                                {
                                                                                    type: "move",
                                                                                    items: [
                                                                                        item,
                                                                                    ],
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        <Move
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Move
                                                                    </button>
                                                                    <button
                                                                        onClick={() =>
                                                                            showDialog(
                                                                                {
                                                                                    type: "share",
                                                                                    item,
                                                                                },
                                                                            )
                                                                        }
                                                                    >
                                                                        <Link
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Share
                                                                        Link
                                                                    </button>
                                                                    <button
                                                                        className="danger-text"
                                                                        onClick={() => {
                                                                            closeItemMenus();
                                                                            onAction(
                                                                                "trash",
                                                                                [
                                                                                    item,
                                                                                ],
                                                                            );
                                                                        }}
                                                                    >
                                                                        <Trash2
                                                                            size={
                                                                                16
                                                                            }
                                                                        />{" "}
                                                                        Move to
                                                                        Trash
                                                                    </button>
                                                                </>
                                                            )}
                                                        </div>
                                                    </details>
                                                )}
                                            </Cell>
                                        </Row>
                                    ))}
                                </CollectionBody>
                            </Collection>
                        </div>
                        {searchPagination &&
                            (searchPagination.offset > 0 ||
                                searchPagination.hasMore) && (
                                <div className="pagination">
                                    <span>
                                        Results {searchPagination.offset + 1}–
                                        {searchPagination.offset +
                                            allItems.length}
                                    </span>
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={!searchPagination.offset}
                                        onClick={searchPagination.onPrevious}
                                    >
                                        Previous Results
                                    </button>
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        disabled={!searchPagination.hasMore}
                                        onClick={searchPagination.onNext}
                                    >
                                        Next Results
                                    </button>
                                </div>
                            )}
                        {!searchPagination && allItems.length > 100 && (
                            <div className="pagination">
                                <span>
                                    Showing {page * 100 + 1}–
                                    {Math.min(
                                        (page + 1) * 100,
                                        allItems.length,
                                    )}{" "}
                                    of {allItems.length}
                                </span>
                                <button
                                    className="icon-button"
                                    disabled={!page}
                                    aria-label="Previous Page"
                                    onClick={() => {
                                        setPage(page - 1);
                                        setSelected(new Set());
                                    }}
                                >
                                    <ChevronLeft size={18} />
                                </button>
                                <button
                                    className="icon-button"
                                    disabled={
                                        (page + 1) * 100 >= allItems.length
                                    }
                                    aria-label="Next Page"
                                    onClick={() => {
                                        setPage(page + 1);
                                        setSelected(new Set());
                                    }}
                                >
                                    <ChevronRight size={18} />
                                </button>
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}
