import {
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    type CSSProperties,
    type MouseEvent as ReactMouseEvent,
    type ReactNode,
} from "react";
import {
    createChange,
    createSelection,
    definePrimitive,
    type ChangeDetails,
} from "@ui-sdk/core";
import { usePrimitive } from "@ui-sdk/react/primitive";
import { useMediaQuery } from "@mantine/hooks";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectItemIndicator,
    SelectTrigger,
    SelectValue,
} from "@ui-sdk/react/select";
import { Toggle, ToggleGroup } from "@ui-sdk/react/toggle";
import {
    Check,
    ChevronDown,
    ListFilter,
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
    LockKeyhole,
    Image,
    FileText,
    Music,
    FolderOpen,
} from "lucide-react";
import type { ActionDialogState } from "./ActionDialog";
import {
    itemPath,
    fileUrl,
    formatBytes,
    formatDate,
    formatShortDate,
    type DriveItem,
    type Directory,
    type NodeType,
    type Share,
} from "../../shared/types";
import { api } from "../../shared/api";
import { shareExpired } from "../shares/SharesView";
import { FileIcon, Spinner, fileKind } from "../../shared/components";
import SortMenu, { type FileSort } from "./SortMenu";
import FileThumbnail from "./FileThumbnail";
import {
    loadServerVideoPositions,
    readLocalVideoPositions,
    videoPositionChange,
    VIDEO_POSITION_CHANGED,
    type VideoPosition,
    type VideoProgressMode,
} from "../previews/video/videoWatchProgress";
import { thumbnailCandidate } from "./thumbnails";
import "./FileBrowser.css";
import { isExtractableArchive } from "./archiveApi";
import { COMPACT_LAYOUT_QUERY } from "../../shared/responsive";
import {
    defaultSortOrder,
    sortDriveItems,
    type SortOrder,
} from "./fileSorting";

const contextAnchors = new WeakMap<
    HTMLDetailsElement,
    {
        invoker: HTMLElement;
        pointer?: { x: number; y: number };
        originalBounds: DOMRect;
    }
>();

function itemMenuInvoker(details: HTMLDetailsElement) {
    return (
        contextAnchors.get(details)?.invoker ?? details.querySelector("summary")
    );
}

function positionItemMenu(details: HTMLDetailsElement) {
    const panel = details.querySelector<HTMLElement>(".action-menu");
    if (!panel || !panel.showPopover) return;
    if (!details.open) {
        contextAnchors.delete(details);
        if (panel.matches(":popover-open")) panel.hidePopover();
        return;
    }
    const bounds = itemMenuInvoker(details)?.getBoundingClientRect();
    if (!bounds) return;
    const context = contextAnchors.get(details);
    panel.style.maxHeight = Math.max(0, window.innerHeight - 24) + "px";
    if (!panel.matches(":popover-open")) panel.showPopover();
    const menu = panel.getBoundingClientRect();
    const left =
        context?.pointer?.x ??
        (context ? bounds.left : bounds.right - menu.width);
    const bottom = context?.pointer?.y ?? bounds.bottom;
    const top = context?.pointer?.y ?? bounds.top;
    const roomBelow = window.innerHeight - bottom - 18;
    panel.style.left =
        Math.max(12, Math.min(left, window.innerWidth - menu.width - 12)) +
        "px";
    panel.style.top =
        Math.max(
            12,
            roomBelow >= menu.height ? bottom + 6 : top - menu.height - 6,
        ) + "px";
}

function ItemActions({
    name,
    contextOnly = false,
    children,
}: {
    name: string;
    contextOnly?: boolean;
    children: ReactNode;
}) {
    return (
        <details
            className={
                "item-actions" + (contextOnly ? " public-context-actions" : "")
            }
            name="file-actions"
            onBlur={({ currentTarget, relatedTarget }) => {
                if (
                    currentTarget.open &&
                    relatedTarget instanceof Node &&
                    !currentTarget.contains(relatedTarget)
                ) {
                    currentTarget.open = false;
                    positionItemMenu(currentTarget);
                }
            }}
            onToggle={({ currentTarget, target }) => {
                if (target === currentTarget) positionItemMenu(currentTarget);
            }}
        >
            <summary
                className="icon-button"
                aria-label={"Actions for " + name}
                hidden={contextOnly}
            >
                <MoreHorizontal size={20} aria-hidden="true" />
            </summary>
            <div className="action-menu" popover="manual">
                {children}
            </div>
        </details>
    );
}

export type FileFilter = "all" | NodeType;
const FILE_FILTERS: [FileFilter, string, typeof Folder | null][] = [
    ["all", "All", null],
    ["folder", "Folders", Folder],
    ["media", "Photos and videos", Image],
    ["docs", "Documents", FileText],
    ["audio", "Audio", Music],
    ["other", "Other", Archive],
];
const EMPTY_FILTER_TITLES: Record<NodeType, string> = {
    folder: "No Folders Here",
    media: "No Photos or Videos Here",
    docs: "No Documents Here",
    audio: "No Audio Here",
    other: "No Other Files Here",
};
const TOUCH_HOLD_MS = 500;
const TOUCH_MOVE_PX = 10;
const ROW_CONTROL =
    "button:not(.item-name-button),a,input,label,summary,details";

const fileSelection = definePrimitive({
    name: "file-selection",
    options: { value: "list", order: "list", onValueChange: "callback" },
    controlled: ["value"],
    state: (options: {
        value: string[];
        order: string[];
        onValueChange: (
            value: string[],
            details: ChangeDetails<"item-press">,
        ) => void;
    }) => ({ value: options.value }),
    sync: (options) => ({ value: options.value }),
    api: (context) => {
        const change = createChange(context);
        return createSelection(context, {
            key: "value",
            order: () => context.options().order,
            multiple: () => true,
            set: (value, details) => change("value", value, details),
        });
    },
    slots: (slot) => ({ root: slot({}) }),
});

export function filterGroup(item: DriveItem): FileFilter {
    const kind = fileKind(item.name, item.type === "folder");
    if (kind === "folder") return "folder";
    if (kind === "image" || kind === "video") return "media";
    if (["pdf", "document", "book", "sheet", "slides"].includes(kind))
        return "docs";
    if (kind === "audio") return "audio";
    return "other";
}
function rowDate(item: DriveItem, recent: boolean) {
    return (recent && item.active_at) || item.upload_date;
}

function MobileFileMetadata({
    item,
    recent,
}: {
    item: DriveItem;
    recent: boolean;
}) {
    const date = formatShortDate(rowDate(item, recent));

    return (
        <span className="file-mobile-meta">
            <span>
                {item.type === "folder" ? "Folder" : formatBytes(item.size)}
            </span>
            {date && (
                <span className="file-mobile-date">
                    <span aria-hidden="true"> · </span>
                    <span className="sr-only">
                        {recent ? "Activity " : "Added "}
                    </span>
                    {date}
                </span>
            )}
        </span>
    );
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
    typePending = false,
    staleTypeRows = false,
    typeError = "",
    onRetryType,
    refreshing = false,
    error,
    isPublic,
    progressMode,
    isTrash,
    isStarred = false,
    isRecent = false,
    onLocate,
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
    paging,
}: {
    directory: Directory;
    listingKey: string;
    shareRefreshKey: string;
    highlightId?: string;
    loading: boolean;
    typePending?: boolean;
    staleTypeRows?: boolean;
    typeError?: string;
    onRetryType?: () => void;
    refreshing?: boolean;
    error: string;
    isPublic: boolean;
    progressMode?: VideoProgressMode;
    isTrash: boolean;
    isStarred?: boolean;
    isRecent?: boolean;
    onLocate?: (item: DriveItem) => void;
    onStar?: (item: DriveItem) => void;
    isSearch: boolean;
    auth: string;
    onReload?: () => void;
    onUpload: () => void;
    onOpen: (item: DriveItem) => void;
    onDialog: (dialog: ActionDialogState) => void;
    onAction: (action: "trash" | "restore", items: DriveItem[]) => void;
    searchPagination?: {
        offset: number;
        hasMore: boolean;
        capped?: boolean;
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
    /** Server-paged folder: rows sit at their absolute position, in server order. */
    paging?: {
        rows: (DriveItem | undefined)[];
        total: number;
        complete: boolean;
        error: string;
        folderKey: string;
        sort: FileSort;
        order: SortOrder;
        filter: FileFilter;
        onSort: (sort: FileSort, order: SortOrder) => void;
        onFilter: (filter: FileFilter) => void;
        onPage?: (page: number) => void;
    };
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
    const [selectionMode, setSelectionMode] = useState<
        "manual" | "shortcut" | null
    >(null);
    const selectButton = useRef<HTMLButtonElement>(null);
    const doneSelectingButton = useRef<HTMLButtonElement>(null);
    const pointerKind = useRef("");
    const touchHold = useRef<{
        id: number;
        x: number;
        y: number;
        timer: number;
    } | null>(null);
    const suppressHoldClick = useRef(false);
    const cancelTouchHold = useCallback(() => {
        if (!touchHold.current) return;
        window.clearTimeout(touchHold.current.timer);
        touchHold.current = null;
    }, []);
    const selectionActive = useRef(false);
    selectionActive.current = !!selectionMode || selected.size > 0;
    const [localSort, setLocalSort] = useState<FileSort>("name");
    const [localOrder, setLocalOrder] = useState<SortOrder>("asc");
    const compact = useMediaQuery(COMPACT_LAYOUT_QUERY, false, {
        getInitialValueInEffect: false,
    });
    const narrow = useMediaQuery("(max-width: 480px)", false, {
        getInitialValueInEffect: false,
    });
    const [layout, setLayout] = useState<"list" | "grid">(() => {
        try {
            if (localStorage.getItem("tgdrive.file-view") === "grid") {
                return "grid";
            }
        } catch {
            // The explorer can still open if browser storage is unavailable.
        }
        return "list";
    });
    const sort = paging?.sort || searchSort || localSort;
    const order = paging?.order || searchOrder || localOrder;
    const setOrder = (value: SortOrder) => {
        if (paging) paging.onSort(sort, value);
        else if (searchPagination) onSearchOrder?.(value);
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
        if (paging) paging.onSort(value, direction);
        else if (searchPagination) {
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
        setSelectionMode(null);
        setPage(0);
        return cancelTouchHold;
    }, [listingKey, cancelTouchHold]);
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
                const invoker = itemMenuInvoker(openMenu);
                hideItemMenus();
                invoker?.focus({ preventScroll: true });
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
            setSelectionMode(null);
            // Selection checkboxes disappear on Escape. Return keyboard focus to a
            // surviving control instead of letting the browser focus the whole scroller.
            requestAnimationFrame(() => {
                const target = item?.isConnected ? item : selectButton.current;
                target?.focus({ preventScroll: true });
            });
        };
        document.addEventListener("keydown", escape);
        return () => document.removeEventListener("keydown", escape);
    }, []);
    const allItems = useMemo(() => {
        if (paging)
            return paging.rows.filter((item): item is DriveItem =>
                Boolean(item),
            );
        const items = (
            searchItems || Object.values(directory.contents || {})
        ).slice();
        return searchPagination ? items : sortDriveItems(items, sort, order);
    }, [
        paging?.rows,
        directory.contents,
        searchItems,
        searchPagination,
        sort,
        order,
    ]);
    const onPage = paging?.onPage;
    const pagedRows = paging?.rows;
    // Rows also change after refreshes, which may drop a page loaded by offset.
    useEffect(() => {
        if (!loading) onPage?.(page);
    }, [onPage, page, pagedRows, loading]);
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
                Math.max(
                    0,
                    Math.ceil((paging?.total ?? allItems.length) / 100) - 1,
                ),
            ),
        );
    }, [allItems, paging?.total]);
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
        const index = (paging?.rows || allItems).findIndex(
            (item) => item?.id === highlightId,
        );
        if (index < 0) return;
        setPage(Math.floor(index / 100));
        setSelected(new Set([highlightId]));
        setSelectionMode(null);
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
    }, [
        allItems,
        paging?.rows,
        listingKey,
        highlightId,
        loading,
        sort,
        order,
        layout,
    ]);
    const [localFilter, setLocalFilter] = useState<FileFilter>("all");
    useEffect(() => setLocalFilter("all"), [listingKey]);
    const filter = paging?.filter || localFilter;
    const setFilter = paging?.onFilter || setLocalFilter;
    const filterCounts = useMemo(() => {
        const counts = new Map<FileFilter, number>();
        for (const item of allItems) {
            const group = filterGroup(item);
            counts.set(group, (counts.get(group) || 0) + 1);
        }
        return counts;
    }, [allItems]);
    // Counts need every row; a partly loaded folder offers every type and
    // remembers the types it found when it was last fully loaded.
    const knownGroups = useRef<{
        key: string;
        counts: Map<FileFilter, number>;
    } | null>(null);
    if (paging?.complete && paging.filter === "all")
        knownGroups.current = { key: paging.folderKey, counts: filterCounts };
    const chipCounts = !paging
        ? filterCounts
        : knownGroups.current?.key === paging.folderKey
          ? knownGroups.current.counts
          : null;
    const availableFilters = FILE_FILTERS.filter(
        ([value]) =>
            !(isRecent && value === "folder") &&
            (value === "all" ||
                !chipCounts ||
                chipCounts.has(value) ||
                value === filter),
    );
    const changeFilter = (value: FileFilter) => {
        if (value === filter) return;
        hideItemMenus();
        setSelected(new Set());
        setSelectionMode(null);
        setFilter(value);
        setPage(0);
    };
    const [showTypeProgress, setShowTypeProgress] = useState(false);
    useEffect(() => {
        setShowTypeProgress(false);
        if (!typePending || typeError) return;
        const timer = window.setTimeout(() => setShowTypeProgress(true), 200);
        return () => window.clearTimeout(timer);
    }, [filter, typePending, typeError]);
    const filteredItems =
        paging || filter === "all"
            ? allItems
            : allItems.filter((item) => filterGroup(item) === filter);
    const itemCount = paging ? paging.total : filteredItems.length;
    const showTypeFilter =
        (!loading || (paging && (filter !== "all" || chipCounts))) &&
        !error &&
        (!chipCounts || chipCounts.size > 1 || filter !== "all");
    const count = isRecent ? allItems.length : itemCount;
    const fileCountLabel = loading
        ? isSearch
            ? "Searching…"
            : "Loading files…"
        : searchPagination
          ? allItems.length
              ? `Showing ${searchPagination.offset + 1}–${searchPagination.offset + allItems.length}${searchPagination.hasMore ? " · More available" : ""}`
              : "No Results"
          : `${count} ${count === 1 ? "item" : "items"}${isRecent && !paging?.complete ? " · More available" : ""}`;
    const pageSlots = paging
        ? Array.from(
              {
                  length: Math.max(
                      0,
                      Math.min(itemCount, (page + 1) * 100) - page * 100,
                  ),
              },
              (_, index) => paging.rows[page * 100 + index],
          )
        : [];
    const pageMissing = pageSlots.some((item) => !item);
    const visibleItems = paging
        ? pageMissing
            ? []
            : (pageSlots as DriveItem[])
        : filteredItems.slice(page * 100, (page + 1) * 100);
    const selectedIds = useMemo(() => [...selected], [selected]);
    const visibleIds = visibleItems.map((item) => item.id);
    const { api: selection } = usePrimitive(fileSelection, {
        value: selectedIds,
        order: visibleIds,
        onValueChange: (ids) => setSelected(new Set(ids)),
    });
    const resultOffset = searchPagination?.offset;
    useEffect(() => {
        selection.setAnchor(null);
    }, [selection, listingKey, page, filter, sort, order, resultOffset]);
    const chosen = allItems.filter((item) => selected.has(item.id));
    const recentItems = useMemo(
        () =>
            allItems
                .filter((item) => item.type === "file" && item.upload_date)
                .sort((a, b) =>
                    String(b.upload_date).localeCompare(String(a.upload_date)),
                )
                .slice(0, 8),
        [allItems],
    );
    const showRecent =
        layout === "grid" &&
        filter === "all" &&
        page === 0 &&
        !isSearch &&
        !isTrash &&
        !isStarred &&
        !isRecent &&
        !isPublic &&
        (!paging || paging.complete) &&
        recentItems.length >= 4 &&
        allItems.length >= 12 &&
        recentItems.some(
            (item, index) =>
                item.id !==
                allItems.filter((entry) => entry.type === "file")[index]?.id,
        );
    const videoIds =
        loading || error || isTrash
            ? []
            : [
                  ...new Set(
                      [...visibleItems, ...(showRecent ? recentItems : [])]
                          .filter(
                              (item) =>
                                  item.type === "file" &&
                                  fileKind(item.name) === "video",
                          )
                          .map((item) => item.id),
                  ),
              ];
    const videoMode = isPublic ? "share" : progressMode;
    const videoKey = JSON.stringify([listingKey, videoMode, videoIds]);
    const [videoProgress, setVideoProgress] = useState<{
        key: string;
        values: Map<string, VideoPosition>;
    }>({ key: "", values: new Map() });
    const visibleVideoProgress =
        videoProgress.key === videoKey ? videoProgress.values : undefined;
    useEffect(() => {
        if (!videoMode || videoMode === "unavailable" || !videoIds.length)
            return;
        const controller = new AbortController();
        const ids = new Set(videoIds);
        const changed = new Set<string>();
        const rows = new Map(
            videoProgress.key === videoKey ? videoProgress.values : [],
        );
        const publish = () =>
            setVideoProgress({ key: videoKey, values: new Map(rows) });
        const saved = (event: Event) => {
            const value = videoPositionChange(event);
            if (
                controller.signal.aborted ||
                !value ||
                value.mode !== videoMode ||
                !ids.has(value.fileId)
            )
                return;
            changed.add(value.fileId);
            rows.set(value.fileId, value.position);
            publish();
        };
        window.addEventListener(VIDEO_POSITION_CHANGED, saved);
        const merge = (snapshot: Record<string, VideoPosition>) => {
            if (controller.signal.aborted) return;
            for (const id of ids) {
                if (changed.has(id)) continue;
                if (Object.hasOwn(snapshot, id)) rows.set(id, snapshot[id]);
                else rows.delete(id);
            }
            publish();
        };
        if (videoMode === "server")
            void loadServerVideoPositions(videoIds, controller.signal)
                .then(merge)
                .catch(() => {});
        else merge(readLocalVideoPositions(videoIds, videoMode));
        return () => {
            controller.abort();
            window.removeEventListener(VIDEO_POSITION_CHANGED, saved);
        };
    }, [videoKey, shareRefreshKey]);
    const closeItemMenus = () => {
        const details =
            document.activeElement?.closest<HTMLDetailsElement>(
                ".item-actions",
            );
        const invoker = details && itemMenuInvoker(details);
        hideItemMenus();
        invoker?.focus({ preventScroll: true });
    };
    const openContextMenu = (
        row: HTMLElement,
        item: DriveItem,
        pointer?: { x: number; y: number },
    ) => {
        const details = row.querySelector<HTMLDetailsElement>(".item-actions");
        const invoker = row.querySelector<HTMLElement>(".item-name-button");
        if (!details || !invoker) return false;
        hideItemMenus();
        contextAnchors.set(details, {
            invoker,
            pointer,
            originalBounds: invoker.getBoundingClientRect(),
        });
        if (!isPublic && !selectionMode)
            selection.choose(item.id, { reason: "item-press" });
        details.open = true;
        positionItemMenu(details);
        details
            .querySelector<HTMLElement>(
                ".action-menu :is(button, a):not([disabled])",
            )
            ?.focus({ preventScroll: true });
        return true;
    };
    const showDialog = (dialog: ActionDialogState) => {
        closeItemMenus();
        onDialog(dialog);
    };
    const openPrimary = (item: DriveItem) => {
        if (isTrash) return;
        setSelected(new Set());
        setSelectionMode(null);
        onOpen(item);
    };
    const clickItem = (
        event: ReactMouseEvent<HTMLElement>,
        item: DriveItem,
    ) => {
        if ((event.target as Element).closest(ROW_CONTROL)) return;
        const mouse = pointerKind.current === "mouse" && event.detail > 0;
        const toggleKey = event.ctrlKey || event.metaKey;
        if (
            mouse &&
            event.detail > 1 &&
            selectionMode !== "manual" &&
            !event.shiftKey &&
            !toggleKey
        )
            return;

        if (mouse) {
            event.currentTarget
                .querySelector<HTMLButtonElement>(".item-name-button")
                ?.focus({ preventScroll: true });
        }

        const details = {
            reason: "item-press" as const,
            event: event.nativeEvent,
        };
        if (!isPublic && mouse && (event.shiftKey || toggleKey)) {
            event.preventDefault();
            const anchor = selection.anchor();
            if (anchor && !visibleIds.includes(anchor))
                selection.setAnchor(null);
            if (event.shiftKey)
                selection.extend(item.id, { ...details, additive: toggleKey });
            else selection.toggle(item.id, details);
            if (selectionMode !== "manual") setSelectionMode("shortcut");
        } else if (!isPublic && selectionMode === "manual") {
            selection.toggle(item.id, details);
        } else if (mouse) {
            if (!isPublic) {
                selection.choose(item.id, details);
                setSelectionMode(null);
            }
        } else {
            openPrimary(item);
        }
    };
    useEffect(() => {
        const close = (event: MouseEvent) => {
            if (!(event.target as Element).closest(".item-actions"))
                hideItemMenus();
        };
        const reposition = (event: Event) => {
            cancelTouchHold();
            if ((event.target as Element)?.closest?.(".action-menu")) return;
            document
                .querySelectorAll<HTMLDetailsElement>(".item-actions[open]")
                .forEach((details) => {
                    const context = contextAnchors.get(details);
                    const bounds =
                        itemMenuInvoker(details)?.getBoundingClientRect();
                    const list = details
                        .closest(".file-list-scroll")
                        ?.getBoundingClientRect();
                    if (
                        !bounds ||
                        (context?.pointer &&
                            (bounds.top !== context.originalBounds.top ||
                                bounds.left !== context.originalBounds.left)) ||
                        bounds.bottom <= Math.max(0, list?.top ?? 0) ||
                        bounds.top >=
                            Math.min(
                                window.innerHeight,
                                list?.bottom ?? window.innerHeight,
                            )
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
    }, [cancelTouchHold]);
    const groupedGrid =
        layout === "grid" &&
        !searchPagination &&
        visibleItems.some((item) => item.type === "folder");
    const gridSection = (item: DriveItem, index: number) =>
        groupedGrid &&
        (index === 0 || visibleItems[index - 1].type !== item.type) ? (
            <li key={"section-" + item.type} className="file-grid-section">
                <h2>{item.type === "folder" ? "Folders" : "Files"}</h2>
            </li>
        ) : null;
    const Collection = layout === "grid" ? "div" : "table";
    const CollectionBody = layout === "grid" ? "ul" : "tbody";
    const Row = layout === "grid" ? "li" : "tr";
    const Cell = layout === "grid" ? "div" : "td";
    return (
        <div
            className="file-browser"
            onPointerDownCapture={(event) => {
                cancelTouchHold();
                pointerKind.current = event.pointerType;
                if (!event.isPrimary) return;
                suppressHoldClick.current = false;
                if (isPublic || selectionMode || event.pointerType !== "touch")
                    return;
                const target = event.target as Element;
                if (target.closest(ROW_CONTROL)) return;
                const row = target.closest<HTMLElement>(".file-row");
                const itemId = row?.dataset.itemId;
                if (!row || !itemId) return;
                touchHold.current = {
                    id: event.pointerId,
                    x: event.clientX,
                    y: event.clientY,
                    timer: window.setTimeout(() => {
                        if (!row.isConnected) return;
                        suppressHoldClick.current = true;
                        hideItemMenus();
                        setSelected(new Set([itemId]));
                        setSelectionMode("manual");
                    }, TOUCH_HOLD_MS),
                };
            }}
            onPointerMoveCapture={(event) => {
                const hold = touchHold.current;
                if (hold?.id !== event.pointerId) return;
                if (
                    Math.hypot(event.clientX - hold.x, event.clientY - hold.y) >
                    TOUCH_MOVE_PX
                )
                    cancelTouchHold();
            }}
            onPointerCancelCapture={cancelTouchHold}
            onPointerUpCapture={cancelTouchHold}
            onContextMenuCapture={(event) => {
                if (touchHold.current || suppressHoldClick.current)
                    event.preventDefault();
            }}
            onClickCapture={(event) => {
                if (!suppressHoldClick.current || event.detail === 0) return;
                suppressHoldClick.current = false;
                event.preventDefault();
                event.stopPropagation();
            }}
        >
            <div
                className="file-toolbar"
                data-selection={selectionMode && !isPublic ? "true" : undefined}
            >
                {header}
                <div className="file-toolbar-controls">
                    {selectionMode && !isPublic ? (
                        <div
                            className="file-selection-bar"
                            role="group"
                            aria-label="Selection actions"
                        >
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
                                ref={doneSelectingButton}
                                type="button"
                                className="text-button"
                                onClick={() => {
                                    setSelected(new Set());
                                    setSelectionMode(null);
                                    requestAnimationFrame(() =>
                                        selectButton.current?.focus({
                                            preventScroll: true,
                                        }),
                                    );
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
                        </div>
                    ) : (
                        <>
                            {!narrow && (
                                <span className="file-item-count">
                                    {fileCountLabel}
                                    {isPublic ? " · Read-only" : ""}
                                </span>
                            )}
                            <div className="file-toolbar-actions">
                                {!isPublic &&
                                    (loading || visibleItems.length > 0) && (
                                        <button
                                            ref={selectButton}
                                            type="button"
                                            className="text-button file-selection-toggle"
                                            disabled={loading || staleTypeRows}
                                            title={
                                                compact
                                                    ? "Select files and folders"
                                                    : "Shift-click for a range. Ctrl-click or Command-click to toggle an item."
                                            }
                                            onClick={() => {
                                                setSelectionMode("manual");
                                                setSelected(new Set());
                                                requestAnimationFrame(() =>
                                                    doneSelectingButton.current?.focus(
                                                        {
                                                            preventScroll: true,
                                                        },
                                                    ),
                                                );
                                            }}
                                        >
                                            <SquareCheck
                                                size={18}
                                                aria-hidden="true"
                                            />
                                            <span>Select</span>
                                        </button>
                                    )}
                                <SortMenu
                                    fixedSort={isRecent}
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
                    <ToggleGroup
                        className="file-view-toggle"
                        aria-label="File view"
                        value={[layout]}
                        deselectable={false}
                        onValueChange={([next]) => {
                            if (next) setView(next);
                        }}
                    >
                        <Toggle
                            value="list"
                            className="icon-button"
                            aria-label="List view"
                            title="List view"
                        >
                            <List size={16} aria-hidden="true" /> List
                        </Toggle>
                        <Toggle
                            value="grid"
                            className="icon-button"
                            aria-label="Grid view"
                            title="Grid view"
                        >
                            <LayoutGrid size={16} aria-hidden="true" /> Grid
                        </Toggle>
                    </ToggleGroup>
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
            {/* Keep the filter in place while server results reload. */}
            {showTypeFilter && (
                <div
                    className="file-filters"
                    role="group"
                    aria-label="Filter by type"
                >
                    {availableFilters.map(([value, label, Icon]) => (
                        <button
                            key={value}
                            type="button"
                            className="file-filter-chip"
                            aria-pressed={filter === value}
                            onClick={() => changeFilter(value)}
                        >
                            {Icon && <Icon size={15} aria-hidden="true" />}
                            {label}
                        </button>
                    ))}
                </div>
            )}
            {(showTypeFilter || narrow) && (
                <div className="file-compact-browse-bar">
                    {showTypeFilter && (
                        <Select
                            items={availableFilters.map(([value, label]) => ({
                                value,
                                label: value === "all" ? "All types" : label,
                            }))}
                            value={filter}
                            onValueChange={(value) => {
                                if (value !== null)
                                    changeFilter(value as FileFilter);
                            }}
                        >
                            <SelectTrigger
                                className="file-type-trigger"
                                aria-label="Type"
                                data-filtered={filter !== "all" || undefined}
                            >
                                <ListFilter size={16} aria-hidden="true" />
                                <SelectValue />
                                <ChevronDown size={14} aria-hidden="true" />
                            </SelectTrigger>
                            <SelectContent
                                className="file-type-options"
                                aria-label="File type"
                                align="start"
                                sideOffset={6}
                                collisionPadding={12}
                            >
                                {availableFilters.map(([value, label]) => (
                                    <SelectItem key={value} value={value}>
                                        <span>
                                            {value === "all"
                                                ? "All types"
                                                : label}
                                        </span>
                                        <SelectItemIndicator>
                                            <Check
                                                size={16}
                                                aria-hidden="true"
                                            />
                                        </SelectItemIndicator>
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                    )}
                    {narrow && (
                        <span className="file-item-count file-mobile-item-count">
                            {fileCountLabel}
                            {isPublic ? " · Read-only" : ""}
                        </span>
                    )}
                </div>
            )}
            {typeError ? (
                <div className="file-type-status">
                    <span role="alert">
                        Could not load{" "}
                        {FILE_FILTERS.find(
                            ([value]) => value === filter,
                        )?.[1].toLowerCase()}
                        . {typeError}
                    </span>
                    <button className="text-button" onClick={onRetryType}>
                        Retry
                    </button>
                    {filter !== "all" && (
                        <button
                            className="text-button"
                            onClick={() => changeFilter("all")}
                        >
                            Clear filter
                        </button>
                    )}
                </div>
            ) : typePending && showTypeProgress ? (
                <p className="file-type-status" role="status">
                    Updating files…
                </p>
            ) : null}
            <div
                className={
                    "file-list-scroll" +
                    (layout === "grid" ? " file-grid-scroll" : "")
                }
                tabIndex={0}
                role="region"
                aria-label="File list"
                aria-busy={typePending && !typeError}
                inert={staleTypeRows}
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
                        {onReload && (
                            <button
                                className="secondary-button"
                                disabled={loading || refreshing}
                                onClick={onReload}
                            >
                                {refreshing && <Spinner />}
                                {refreshing ? "Refreshing…" : "Try Again"}
                            </button>
                        )}
                    </div>
                ) : pageMissing && paging?.error ? (
                    <div className="empty-state">
                        <AlertCircle size={38} />
                        <h2>Could Not Load This Page</h2>
                        <p role="alert">{paging.error}</p>
                        <button
                            className="secondary-button"
                            onClick={() => paging.onPage?.(page)}
                        >
                            Try Again
                        </button>
                        {page > 0 && (
                            <button
                                className="text-button"
                                onClick={() => setPage(page - 1)}
                            >
                                Previous Page
                            </button>
                        )}
                    </div>
                ) : loading || pageMissing ? (
                    <div
                        className="loading-state file-skeleton"
                        data-layout={layout}
                        role="status"
                    >
                        <span className="sr-only">Loading files…</span>
                        {Array.from(
                            { length: layout === "grid" ? 10 : 8 },
                            (_, i) => (
                                <span
                                    key={i}
                                    className="skeleton-item"
                                    aria-hidden="true"
                                >
                                    <span className="skeleton-block" />
                                    <span className="skeleton-line" />
                                    <span className="skeleton-line short" />
                                </span>
                            ),
                        )}
                    </div>
                ) : !staleTypeRows && filter !== "all" && !itemCount ? (
                    <div className="empty-state">
                        <Folder size={46} />
                        <h2>{EMPTY_FILTER_TITLES[filter]}</h2>
                        <p>
                            {isRecent
                                ? "Choose All to see every recent file."
                                : "Choose All to see everything in this folder."}
                        </p>
                        <button
                            className="secondary-button"
                            onClick={() => changeFilter("all")}
                        >
                            Clear filter
                        </button>
                    </div>
                ) : allItems.length === 0 ? (
                    <div className="empty-state">
                        <Folder size={46} />
                        <h2>
                            {isRecent
                                ? "No Recent Files Yet"
                                : isStarred
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
                            {isRecent
                                ? "Files you open or upload appear here."
                                : isStarred
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
                        {!isPublic &&
                            !isTrash &&
                            !isSearch &&
                            !isStarred &&
                            !isRecent && (
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
                        {showRecent && (
                            <section
                                className="recent-files"
                                aria-labelledby="recent-files-title"
                            >
                                <h2 id="recent-files-title">Recently added</h2>
                                <ul>
                                    {recentItems.map((item, index) => (
                                        <li
                                            key={item.id}
                                            style={
                                                {
                                                    "--i": index,
                                                } as CSSProperties
                                            }
                                        >
                                            <button
                                                type="button"
                                                className="recent-file"
                                                aria-label={"Open " + item.name}
                                                onClick={() => onOpen(item)}
                                            >
                                                <FileThumbnail
                                                    item={item}
                                                    progress={visibleVideoProgress?.get(
                                                        item.id,
                                                    )}
                                                    auth={auth}
                                                />
                                                <span className="recent-file-name">
                                                    <FileIcon
                                                        name={item.name}
                                                    />
                                                    <span>{item.name}</span>
                                                </span>
                                                <span className="recent-file-meta">
                                                    {formatBytes(item.size) +
                                                        " · " +
                                                        formatShortDate(
                                                            item.upload_date,
                                                        )}
                                                </span>
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            </section>
                        )}
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
                                                        disabled={isRecent}
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
                                                    aria-label={
                                                        isRecent
                                                            ? undefined
                                                            : "Sort by Date Added"
                                                    }
                                                    disabled={isRecent}
                                                    onClick={() => {
                                                        setSort(
                                                            "modified",
                                                            true,
                                                        );
                                                        setPage(0);
                                                    }}
                                                >
                                                    {isRecent
                                                        ? "Activity"
                                                        : "Added"}
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
                                                    disabled={isRecent}
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
                                    {visibleItems.map((item, index) => [
                                        gridSection(item, index),
                                        <Row
                                            key={item.id}
                                            className={
                                                "file-row " +
                                                (selected.has(item.id)
                                                    ? "selected"
                                                    : "")
                                            }
                                            data-item-id={item.id}
                                            data-kind={item.type}
                                            style={
                                                {
                                                    "--i": Math.min(index, 14),
                                                } as CSSProperties
                                            }
                                            onClick={(event) =>
                                                clickItem(event, item)
                                            }
                                            onContextMenu={(event) => {
                                                if (
                                                    pointerKind.current ===
                                                        "touch" ||
                                                    (
                                                        event.target as Element
                                                    ).closest(
                                                        "a,input,label,.action-menu",
                                                    )
                                                )
                                                    return;
                                                if (
                                                    openContextMenu(
                                                        event.currentTarget,
                                                        item,
                                                        {
                                                            x: event.clientX,
                                                            y: event.clientY,
                                                        },
                                                    )
                                                )
                                                    event.preventDefault();
                                            }}
                                            onKeyDown={(event) => {
                                                if (
                                                    (event.key ===
                                                        "ContextMenu" ||
                                                        (event.shiftKey &&
                                                            event.key ===
                                                                "F10")) &&
                                                    (
                                                        event.target as Element
                                                    ).closest(
                                                        ".item-name-button, summary",
                                                    ) &&
                                                    openContextMenu(
                                                        event.currentTarget,
                                                        item,
                                                    )
                                                )
                                                    event.preventDefault();
                                            }}
                                            onDoubleClick={(event) => {
                                                if (
                                                    selectionMode ===
                                                        "manual" ||
                                                    event.shiftKey ||
                                                    event.ctrlKey ||
                                                    event.metaKey ||
                                                    pointerKind.current !==
                                                        "mouse" ||
                                                    (
                                                        event.target as Element
                                                    ).closest(ROW_CONTROL)
                                                )
                                                    return;
                                                openPrimary(item);
                                            }}
                                        >
                                            {layout === "grid" &&
                                                item.type !== "folder" && (
                                                    <FileThumbnail
                                                        item={item}
                                                        progress={visibleVideoProgress?.get(
                                                            item.id,
                                                        )}
                                                        auth={auth}
                                                        disabled={isTrash}
                                                    />
                                                )}
                                            <Cell className="name-cell">
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
                                                                                selection.toggle(
                                                                                    item.id,
                                                                                    {
                                                                                        reason: "item-press",
                                                                                    },
                                                                                )
                                                                            }
                                                                        />
                                                                    </label>
                                                                )}
                                                            </span>
                                                        )}
                                                    {layout === "list" &&
                                                    thumbnailCandidate(item) ? (
                                                        <FileThumbnail
                                                            item={item}
                                                            progress={visibleVideoProgress?.get(
                                                                item.id,
                                                            )}
                                                            auth={auth}
                                                            disabled={isTrash}
                                                            compact
                                                        />
                                                    ) : (
                                                        <FileIcon
                                                            name={item.name}
                                                            folder={
                                                                item.type ===
                                                                "folder"
                                                            }
                                                            inline={
                                                                layout ===
                                                                "grid"
                                                            }
                                                        />
                                                    )}
                                                    <div className="file-name-details">
                                                        <div className="file-title-line">
                                                            <button
                                                                className="file-name item-name-button"
                                                                aria-keyshortcuts="Shift+F10"
                                                                title={
                                                                    item.name
                                                                }
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
                                                                                <span className="file-status-label file-status-starred">
                                                                                    Starred
                                                                                </span>
                                                                            )}
                                                                        {!isTrash &&
                                                                            sharing(
                                                                                item,
                                                                            ) && (
                                                                                <span
                                                                                    className="file-status-label file-status-shared"
                                                                                    title={sharing(
                                                                                        item,
                                                                                    )}
                                                                                >
                                                                                    Shared
                                                                                </span>
                                                                            )}
                                                                    </span>
                                                                )}
                                                        </div>
                                                        {layout === "list" &&
                                                            compact && (
                                                                <MobileFileMetadata
                                                                    item={item}
                                                                    recent={
                                                                        isRecent
                                                                    }
                                                                />
                                                            )}
                                                        {(isSearch ||
                                                            isTrash ||
                                                            isStarred ||
                                                            isRecent) && (
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
                                                        (layout === "grid" ||
                                                            !compact) && (
                                                            <span className="sr-only">
                                                                Folder
                                                            </span>
                                                        )}
                                                </div>
                                            </Cell>
                                            {layout === "list" && (
                                                <>
                                                    <Cell className="date-cell">
                                                        {formatDate(
                                                            rowDate(
                                                                item,
                                                                isRecent,
                                                            ),
                                                        )}
                                                    </Cell>
                                                    <Cell className="size-cell">
                                                        {item.type === "folder"
                                                            ? "—"
                                                            : formatBytes(
                                                                  item.size,
                                                              )}
                                                    </Cell>
                                                </>
                                            )}
                                            <Cell className="actions-cell">
                                                {isPublic ? (
                                                    <>
                                                        <a
                                                            className="icon-button"
                                                            href={
                                                                item.type ===
                                                                "file"
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
                                                                item.type ===
                                                                "file"
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
                                                                    ? (
                                                                          event,
                                                                      ) => {
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
                                                        <ItemActions
                                                            name={item.name}
                                                            contextOnly
                                                        >
                                                            <button
                                                                onClick={() => {
                                                                    closeItemMenus();
                                                                    onOpen(
                                                                        item,
                                                                    );
                                                                }}
                                                            >
                                                                <Eye
                                                                    size={16}
                                                                    aria-hidden="true"
                                                                />{" "}
                                                                Open
                                                            </button>
                                                            {item.type ===
                                                                "file" && (
                                                                <a
                                                                    href={fileUrl(
                                                                        itemPath(
                                                                            item,
                                                                        ),
                                                                        auth,
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
                                                                        aria-hidden="true"
                                                                    />{" "}
                                                                    Download
                                                                </a>
                                                            )}
                                                        </ItemActions>
                                                    </>
                                                ) : (
                                                    <ItemActions
                                                        name={item.name}
                                                    >
                                                        {compact && (
                                                            <p className="file-action-name">
                                                                {item.name}
                                                            </p>
                                                        )}
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
                                                                <hr />
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
                                                                <hr />
                                                                {onLocate && (
                                                                    <button
                                                                        onClick={() => {
                                                                            closeItemMenus();
                                                                            onLocate(
                                                                                item,
                                                                            );
                                                                        }}
                                                                    >
                                                                        <FolderOpen
                                                                            size={
                                                                                16
                                                                            }
                                                                            aria-hidden="true"
                                                                        />{" "}
                                                                        Show in
                                                                        Folder
                                                                    </button>
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
                                                                {item.type ===
                                                                    "file" &&
                                                                    item.name
                                                                        .toLowerCase()
                                                                        .endsWith(
                                                                            ".pdf",
                                                                        ) && (
                                                                        <button
                                                                            disabled={
                                                                                item.read_only ||
                                                                                directory.read_only
                                                                            }
                                                                            onClick={() =>
                                                                                showDialog(
                                                                                    {
                                                                                        type: "pdf-password",
                                                                                        item,
                                                                                    },
                                                                                )
                                                                            }
                                                                        >
                                                                            <LockKeyhole
                                                                                size={
                                                                                    16
                                                                                }
                                                                                aria-hidden="true"
                                                                            />{" "}
                                                                            PDF
                                                                            password
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
                                                                    Create ZIP
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
                                                                    Share Link
                                                                </button>
                                                                <hr />
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
                                                    </ItemActions>
                                                )}
                                            </Cell>
                                        </Row>,
                                    ])}
                                </CollectionBody>
                            </Collection>
                        </div>
                        {searchPagination?.capped && (
                            <p className="search-capped" role="note">
                                More than 1,000 matches. Add a word to narrow
                                the search.
                            </p>
                        )}
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
                        {!searchPagination && itemCount > 100 && (
                            <div className="pagination">
                                <span>
                                    Showing {page * 100 + 1}–
                                    {Math.min((page + 1) * 100, itemCount)}
                                    {isRecent ? "" : " of " + itemCount}
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
                                    disabled={(page + 1) * 100 >= itemCount}
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
