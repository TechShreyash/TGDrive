import BrandLogo from "../shared/BrandLogo";
import GitHubMark from "../shared/GitHubMark";
import ThemeControl from "../shared/ThemeControl";
import {
    useCallback,
    useEffect,
    useLayoutEffect,
    useMemo,
    useRef,
    useState,
    useSyncExternalStore,
    type FormEvent,
} from "react";
import {
    Star,
    Clock,
    FolderClosed,
    ArrowDownUp,
    ChartNoAxesCombined,
    Server,
    Database,
    Gauge,
    FolderPlus,
    FolderUp,
    HardDrive,
    Search,
    Trash2,
    Upload,
    Plus,
    Link,
    ChevronRight,
    BookOpen,
    X,
    Plug,
    Globe,
    Magnet,
    ShieldCheck,
    ShieldAlert,
    Menu,
    SlidersHorizontal,
} from "lucide-react";
import { api, ApiError, errorMessage } from "../shared/api";
import {
    itemLink,
    itemPath,
    formatBytes,
    setTimezone,
    type DriveItem,
    type Directory,
    type NodeQuery,
    type View,
} from "../shared/types";
import { transfers, type Transfer } from "../features/transfers/transfers";
import {
    transferFolderDestination,
    transferTaskId,
} from "../features/transfers/transferTasks";
import { needsDeviceUpload } from "../features/transfers/deviceUploadStatus";
import {
    createListingRefreshQueue,
    reconcileDirectory,
    reconcileDriveItems,
} from "../features/files/listingUpdates";
import {
    applyPage,
    emptyFolderPages,
    folderComplete,
    loadPrefix,
    pageRequest,
    reconcilePages,
    removeRows,
    type FetchPage,
    type FolderPages,
} from "../features/files/folderPages";
import { Modal, Spinner, ToastNotice as Toast } from "../shared/components";
import Login from "./Login";
import SharesView from "../features/shares/SharesView";
import StorageView from "../features/settings/StorageView";
import StartNewDrive from "../features/settings/StartNewDrive";
import SettingsManagement from "../features/settings/SettingsManagement";
import PerformanceSettings from "../features/performance/PerformanceSettings";
import Preloads from "../features/performance/Preloads";
import PerformancePanel from "../features/performance/PerformancePanel";
import FileBrowser, { type FileFilter } from "../features/files/FileBrowser";
import { COMPACT_LAYOUT_QUERY } from "../shared/responsive";
import type { FileSort } from "../features/files/SortMenu";
import type { SortOrder } from "../features/files/fileSorting";
import ActionDialog, {
    type ActionDialogState,
} from "../features/files/ActionDialog";
import TransferPanel from "../features/transfers/TransferPanel";
import TransferHistory from "../features/transfers/TransferHistory";
import FolderUpload from "../features/transfers/FolderUpload";
import DeletionNotice from "../features/transfers/DeletionNotice";
import SidebarLogout from "./SidebarLogout";
import ChannelBackup from "../features/backup/ChannelBackup";
import BotModeSettings from "../features/settings/BotModeSettings";
import { useManualRefresh } from "../shared/useManualRefresh";
import { startStatusPolling } from "./statusPolling";
function getLocation() {
    const url = new URL(window.location.href);
    const path = url.searchParams.get("path") || "/";
    const requested = url.searchParams.get("view");
    const section = url.searchParams.get("section");
    const view: View = path.startsWith("/share_")
        ? "files"
        : path === "/trash"
          ? "trash"
          : [
                  "activity",
                  "recent",
                  "starred",
                  "shares",
                  "connections",
                  "storage",
                  "settings",
                  "preloads",
                  "performance",
              ].includes(requested || "")
            ? (requested as View)
            : "files";
    return {
        path,
        auth: url.searchParams.get("auth") || "",
        view,
        settingsSection: ["cache", "transfers", "regional"].includes(
            section || "",
        )
            ? section!
            : "cache",
        previewId: url.searchParams.get("preview") || "",
        highlightId:
            typeof window.history.state?.tgdriveHighlight === "string"
                ? window.history.state.tgdriveHighlight
                : "",
    };
}
const TOOL_VIEWS: View[] = [
    "performance",
    "preloads",
    "settings",
    "connections",
    "storage",
];
const ROOT_CRUMB = { path: "/", name: "My Files" };
function folderDirectory(pages: FolderPages): Directory {
    const folder = pages.folder;
    return {
        name: folder?.name,
        read_only: folder?.read_only,
        breadcrumbs: folder?.breadcrumbs,
        contents: Object.fromEntries(
            pages.rows.flatMap((item) => (item ? [[item.id, item]] : [])),
        ),
    };
}
const ADMIN_SETTING_TITLES: Record<string, string> = {
    cache: "Cache & preloading",
    transfers: "Uploads & Telegram",
    regional: "Regional settings",
};
function searchHistoryOffset() {
    const value = window.history.state?.tgdriveSearchOffset;
    return Number.isSafeInteger(value) && value >= 0 && value % 100 === 0
        ? value
        : 0;
}
export default function App() {
    const [location, setLocation] = useState(getLocation);
    const [toolsOpen, setToolsOpen] = useState(() =>
        TOOL_VIEWS.includes(location.view),
    );
    useEffect(() => {
        setToolsOpen(TOOL_VIEWS.includes(location.view));
    }, [location.view, location.settingsSection]);
    const [authenticated, setAuthenticated] = useState<boolean | null>(null);
    const [admissionPaused, setAdmissionPaused] = useState(false);
    const [telegramWarning, setTelegramWarning] = useState("");
    const [config, setConfig] = useState<{
        max_file_size: number;
        demo_data?: boolean;
        demo_mode?: boolean;
        demo_read_only?: boolean;
        demo_admin?: boolean;
        recovery_only?: boolean;
    }>({ max_file_size: 0, recovery_only: false });
    const demoReadOnly = Boolean(config.demo_read_only ?? config.demo_mode);
    const [configLoad, setConfigLoad] = useState<"loading" | "ready" | "error">(
        "loading",
    );
    const [configError, setConfigError] = useState("");
    const [configReady, setConfigReady] = useState(false);
    const configGeneration = useRef(0);
    const loadConfig = useCallback(async () => {
        const generation = ++configGeneration.current;
        // Refreshing the same session must not unmount unsaved settings drafts.
        setConfigLoad((current) => (current === "ready" ? current : "loading"));
        setConfigError("");
        try {
            const value = await api.config();
            if (generation !== configGeneration.current) return;
            setConfig(value);
            setTimezone(value.timezone || "Asia/Kolkata");
            setConfigLoad("ready");
            setConfigReady(true);
        } catch (cause) {
            if (generation !== configGeneration.current) return;
            setConfigError(errorMessage(cause));
            setConfigLoad("error");
        }
    }, []);
    const [plainDirectory, setDirectory] = useState<Directory>({
        contents: {},
    });
    const [pages, setPages] = useState<FolderPages>(emptyFolderPages);
    // The listing the cached pages belong to; stale while another one loads.
    const [pagesKey, setPagesKey] = useState("");
    const pagesRef = useRef(pages);
    pagesRef.current = pages;
    // The current folder listing's page fetcher; null while it first loads.
    const pageSource = useRef<{
        fetch: FetchPage;
        pending: Set<string>;
    } | null>(null);
    const folderPage = useRef(0);
    const [folderSort, setFolderSort] = useState<FileSort>("name");
    const [folderOrder, setFolderOrder] = useState<SortOrder>("asc");
    const [folderFilterState, setFolderFilterState] = useState<{
        path: string;
        filter: FileFilter;
    }>({ path: "", filter: "all" });
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState("");
    const [refresh, setRefresh] = useState(0);
    const [metadataRefresh, setMetadataRefresh] = useState(0);
    const [dialog, setDialog] = useState<ActionDialogState | null>(null);
    const [demoFilesReadOnly, setDemoFilesReadOnly] = useState(false);
    const [query, setQuery] = useState("");
    const [scopedQuery, setScopedQuery] = useState("");
    const [searchOffset, setSearchOffset] = useState(searchHistoryOffset);
    const [searchSort, setSearchSort] = useState<FileSort>("name");
    const [searchOrder, setSearchOrder] = useState<SortOrder>("asc");
    const [searchMore, setSearchMore] = useState(false);
    const [searchCapped, setSearchCapped] = useState(false);
    const [searchItems, setSearchItems] = useState<DriveItem[]>([]);
    const [nextSearchOffset, setNextSearchOffset] = useState<number | null>(
        null,
    );
    const searchInput = useRef<HTMLInputElement>(null);
    const searchTrigger = useRef<HTMLButtonElement>(null);
    const [searchExpanded, setSearchExpanded] = useState(false);
    const requestedSearchFocus = useRef<{ select: boolean } | null>(null);
    const revealSearch = useCallback((select = false) => {
        setSearchExpanded(true);
        const input = searchInput.current;
        if (input && input.getClientRects().length) {
            input.focus();
            if (select) input.select();
        } else requestedSearchFocus.current = { select };
    }, []);
    const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
        undefined,
    );
    const searchComposing = useRef(false);
    const ownSearchNavigation = useRef<string | null>(null);
    const searchOrigin = useRef<string | null>(
        typeof window.history.state?.tgdriveSearchOrigin === "string"
            ? window.history.state.tgdriveSearchOrigin
            : null,
    );
    const lastFolder = useRef("/");
    const [toast, setToast] = useState<{
        message: string;
        error: boolean;
    } | null>(null);
    const [newOpen, setNewOpen] = useState(false);
    useEffect(() => {
        if (!newOpen) return;
        const menu = document.getElementById("new-menu");
        const trigger = document.activeElement;
        const buttons = () =>
            Array.from(
                menu?.querySelectorAll<HTMLButtonElement>(
                    "button:not(:disabled)",
                ) || [],
            );
        buttons()[0]?.focus();
        const keydown = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setNewOpen(false);
                if (trigger instanceof HTMLElement) trigger.focus();
                return;
            }
            if (!menu?.contains(event.target as Node)) return;
            if (event.key === "Tab") {
                setNewOpen(false);
                if (trigger instanceof HTMLElement) trigger.focus();
                return;
            }
            const actions = buttons();
            const current = actions.indexOf(
                document.activeElement as HTMLButtonElement,
            );
            let next = current;
            if (event.key === "ArrowDown")
                next = (current + 1) % actions.length;
            else if (event.key === "ArrowUp")
                next = (current + actions.length - 1) % actions.length;
            else if (event.key === "Home") next = 0;
            else if (event.key === "End") next = actions.length - 1;
            else return;
            event.preventDefault();
            actions[next]?.focus();
        };
        document.addEventListener("keydown", keydown, true);
        return () => document.removeEventListener("keydown", keydown, true);
    }, [newOpen]);
    const [mobileNav, setMobileNav] = useState(false);
    useEffect(() => {
        if (!mobileNav) return;
        const navigation = document.getElementById("drive-navigation");
        const opener = document.getElementById("navigation-toggle");
        if (navigation) navigation.scrollTop = 0;
        const controls = () =>
            Array.from(
                navigation?.querySelectorAll<HTMLElement>(
                    "a[href],button:not(:disabled),summary",
                ) || [],
            ).filter((element) => element.getClientRects().length > 0);
        (
            navigation?.querySelector<HTMLElement>('[aria-current="page"]') ||
            controls()[0]
        )?.focus();
        const keydown = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setMobileNav(false);
            } else if (event.key === "Tab") {
                const available = controls();
                const first = available[0],
                    last = available.at(-1);
                if (
                    event.shiftKey &&
                    (document.activeElement === first ||
                        !navigation?.contains(document.activeElement))
                ) {
                    event.preventDefault();
                    last?.focus();
                } else if (
                    !event.shiftKey &&
                    (document.activeElement === last ||
                        !navigation?.contains(document.activeElement))
                ) {
                    event.preventDefault();
                    first?.focus();
                }
            }
        };
        document.addEventListener("keydown", keydown, true);
        return () => {
            document.removeEventListener("keydown", keydown, true);
            if (opener?.isConnected) opener.focus({ preventScroll: true });
        };
    }, [mobileNav]);

    const [mobile, setMobile] = useState(
        () => window.matchMedia(COMPACT_LAYOUT_QUERY).matches,
    );
    useEffect(() => {
        const media = window.matchMedia(COMPACT_LAYOUT_QUERY);
        const update = () => {
            setMobile(media.matches);
            if (!media.matches) setMobileNav(false);
        };
        media.addEventListener("change", update);
        return () => media.removeEventListener("change", update);
    }, []);
    const [crumbs, setCrumbs] = useState([ROOT_CRUMB]);
    const [crumbsExpanded, setCrumbsExpanded] = useState(false);
    useEffect(() => setCrumbsExpanded(false), [location.path]);
    const fileInput = useRef<HTMLInputElement>(null);
    const folderInput = useRef<HTMLInputElement>(null);
    const [folderSupported] = useState(
        () => "webkitdirectory" in document.createElement("input"),
    );
    const folderDestination = useRef({ path: "/", name: "My Files" });
    const [folderSelection, setFolderSelection] = useState<{
        files: File[];
        destination: string;
        name: string;
    } | null>(null);
    const currentPath = location.path;
    const isPublic = currentPath.startsWith("/share_");
    const isSearch = currentPath.startsWith("/search_");
    const view = location.view;
    const searchLabel =
        view === "files"
            ? "Search Files and Folders"
            : view === "trash"
              ? "Search Trash"
              : view === "starred"
                ? "Search Starred"
                : view === "activity"
                  ? "Search Transfers"
                  : view === "shares"
                    ? "Search Shared Links"
                    : view === "preloads"
                      ? "Search cached files"
                      : "";
    useLayoutEffect(() => {
        const requested = requestedSearchFocus.current;
        const input = searchInput.current;
        if (!requested || !input?.getClientRects().length) return;
        requestedSearchFocus.current = null;
        input.focus();
        if (requested.select) input.select();
    }, [searchExpanded, mobile, query, searchLabel]);
    const isRecent = view === "recent";
    const isFiles =
        view === "files" || view === "trash" || view === "starred" || isRecent;
    const isPaged = (view === "files" && !isSearch) || isRecent;
    const folderKey = view + currentPath + location.auth;
    // A type filter belongs to the folder it was chosen in.
    const folderFilter =
        folderFilterState.path === folderKey ? folderFilterState.filter : "all";
    const directory = useMemo(
        () => (isPaged ? folderDirectory(pages) : plainDirectory),
        [isPaged, pages, plainDirectory],
    );
    const listingKey = JSON.stringify([
        authenticated,
        view,
        currentPath,
        location.auth,
        isSearch ? searchOffset : 0,
        isSearch ? searchSort : "",
        isSearch ? searchOrder : "",
        ["trash", "starred"].includes(view) ? scopedQuery : "",
        isPaged ? [folderSort, folderOrder, folderFilter] : "",
    ]);
    const loadedListing = useRef<string | null>(null);
    const listingRefresh = useRef<ReturnType<
        typeof createListingRefreshQueue
    > | null>(null);
    const workspace = useRef<HTMLElement>(null);
    useLayoutEffect(() => {
        if (workspace.current) workspace.current.scrollTop = 0;
    }, [currentPath, view, location.settingsSection, searchOffset]);
    // Opacity only: a moving workspace would shift taps and fixed menus.
    useEffect(() => {
        if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        workspace.current?.animate?.([{ opacity: 0 }, { opacity: 1 }], {
            duration: 200,
            easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
        });
    }, [view, location.settingsSection]);
    const items = useSyncExternalStore(transfers.subscribe, transfers.snapshot);
    const deletionProgress = useSyncExternalStore(
        transfers.subscribe,
        transfers.deletionSnapshot,
    );
    const activeCount = new Set(
        items
            .filter(
                (item) =>
                    !["completed", "error", "cancelled"].includes(item.state),
            )
            .map(transferTaskId),
    ).size;
    const mobileUploadIssues = items.some(
        (item) =>
            item.initiatedHere &&
            ["error", "awaiting_file"].includes(item.state),
    );
    const mobileActiveCount = new Set(
        items
            .filter(
                (item) =>
                    ![
                        "completed",
                        "error",
                        "cancelled",
                        "awaiting_file",
                    ].includes(item.state),
            )
            .map(transferTaskId),
    ).size;
    const deviceUploadPending =
        authenticated === true && !isPublic && items.some(needsDeviceUpload);
    useEffect(() => {
        if (!deviceUploadPending) return;
        const beforeUnload = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = "";
        };
        window.addEventListener("beforeunload", beforeUnload);
        return () => window.removeEventListener("beforeunload", beforeUnload);
    }, [deviceUploadPending]);
    const notify = useCallback(
        (message: string, error = false) => setToast({ message, error }),
        [],
    );
    const endSession = useCallback(() => {
        setConfig((value) => ({
            ...value,
            demo_read_only: Boolean(value.demo_mode),
            demo_admin: false,
        }));
        setAuthenticated(false);
        setDialog(null);
        setFolderSelection(null);
        setDemoFilesReadOnly(false);
        setConfigLoad("loading");
        setConfigReady(false);
        void loadConfig();
    }, [loadConfig]);
    const listingRefreshAction = useManualRefresh(notify);
    useEffect(() => {
        if (
            !deletionProgress.completed ||
            deletionProgress.jobs.some(
                (job) =>
                    ![
                        "completed",
                        "cancelled",
                        "error",
                        "failed",
                        "interrupted",
                    ].includes(job.status),
            )
        )
            return;
        notify(
            "Permanently deleted " +
                deletionProgress.completed +
                (deletionProgress.completed === 1 ? " item." : " items."),
        );
        // Acknowledge the result once so later polls and page changes cannot replay it.
        transfers.dismissDeletionResult();
    }, [deletionProgress, notify]);
    const reload = useCallback(() => setRefresh((value) => value + 1), []);
    const invalidateMetadata = useCallback(() => {
        setMetadataRefresh((value) => value + 1);
    }, []);
    const reloadMetadata = useCallback(() => {
        invalidateMetadata();
        reload();
    }, [invalidateMetadata, reload]);
    const navigate = useCallback(
        (
            path: string,
            view: View = "files",
            auth = "",
            replace = false,
            state: Record<string, unknown> = {},
            settingsSection = "cache",
        ) => {
            clearTimeout(searchTimer.current);
            const params = new URLSearchParams();
            if (path !== "/") params.set("path", itemLink(path));
            if (view !== "files" && view !== "trash") params.set("view", view);
            if (view === "settings") params.set("section", settingsSection);
            if (auth) params.set("auth", auth);
            const target = "/" + (params.size ? "?" + params.toString() : "");
            if (replace) window.history.replaceState(state, "", target);
            else window.history.pushState(state, "", target);
            setLocation(getLocation());
            setDialog(null);
            setNewOpen(false);
            setMobileNav(false);
        },
        [],
    );
    useEffect(() => {
        const pop = () => {
            clearTimeout(searchTimer.current);
            ownSearchNavigation.current = null;
            searchOrigin.current =
                typeof window.history.state?.tgdriveSearchOrigin === "string"
                    ? window.history.state.tgdriveSearchOrigin
                    : null;
            setSearchOffset(searchHistoryOffset());
            setLocation(getLocation());
            setDialog(null);
        };
        window.addEventListener("popstate", pop);
        return () => window.removeEventListener("popstate", pop);
    }, []);
    useEffect(() => {
        setConfigLoad("loading");
        setConfigReady(false);
        void loadConfig();
        return () => {
            ++configGeneration.current;
        };
    }, [isPublic, loadConfig]);
    useEffect(() => {
        if (isPublic) {
            setAuthenticated(false);
            return;
        }
        void api
            .session()
            .then((result) => setAuthenticated(result.authenticated))
            .catch(() => setAuthenticated(false));
    }, [isPublic]);
    useEffect(() => {
        if (authenticated !== true || isPublic) {
            setAdmissionPaused(false);
            setTelegramWarning("");
            return;
        }
        return startStatusPolling((status) => {
            setAdmissionPaused(Boolean(status.admission_paused));
            setTelegramWarning(
                status.telegram_issues?.[0]?.detail ||
                    (status.telegram_ready === false &&
                    !status.demo &&
                    !status.recovery_only
                        ? "No usable Telegram account is connected. Open Connections and Refresh to check bot tokens, Premium sessions and channel permissions. Existing files and saved upload progress are preserved."
                        : ""),
            );
        });
    }, [authenticated, isPublic]);
    useEffect(() => {
        const saved = (event: Event) => {
            const detail = (
                event as CustomEvent<{ admission_paused?: boolean }>
            ).detail;
            setAdmissionPaused(Boolean(detail?.admission_paused));
            setNewOpen(false);
        };
        window.addEventListener("tgdrive:settings-saved", saved);
        return () =>
            window.removeEventListener("tgdrive:settings-saved", saved);
    }, []);
    useEffect(() => {
        transfers.setAdmissionPaused(admissionPaused);
    }, [admissionPaused]);
    useEffect(() => {
        transfers.setEnabled(
            authenticated === true && !isPublic && !config.recovery_only,
        );
        return () => transfers.setEnabled(false);
    }, [authenticated, isPublic, config.recovery_only]);
    useEffect(() => {
        const expired = () => {
            endSession();
            notify("Your session expired. Log in to continue.", true);
        };
        window.addEventListener("tgdrive:auth-required", expired);
        window.addEventListener("tgdrive:files-changed", reload);
        return () => {
            window.removeEventListener("tgdrive:auth-required", expired);
            window.removeEventListener("tgdrive:files-changed", reload);
        };
    }, [endSession, notify, reload]);
    useEffect(() => {
        const refused = () => {
            setDialog(null);
            setFolderSelection(null);
            setNewOpen(false);
            setDemoFilesReadOnly(true);
        };
        window.addEventListener("tgdrive:demo-files-read-only", refused);
        return () =>
            window.removeEventListener("tgdrive:demo-files-read-only", refused);
    }, []);
    useEffect(() => {
        if (ownSearchNavigation.current === currentPath) {
            ownSearchNavigation.current = null;
        } else {
            setQuery(isSearch ? currentPath.slice(8) : "");
            setScopedQuery("");
            setSearchExpanded(false);
        }
        if (!isSearch && !isPublic && view === "files")
            lastFolder.current = currentPath;
    }, [isSearch, currentPath, isPublic, view]);
    useEffect(() => {
        if (!isSearch) return;
        window.history.replaceState(
            { ...window.history.state, tgdriveSearchOffset: searchOffset },
            "",
            window.location.href,
        );
    }, [isSearch, searchOffset]);
    useEffect(() => {
        const shortcut = (event: KeyboardEvent) => {
            if (
                isPublic ||
                !searchLabel ||
                authenticated !== true ||
                document.querySelector("dialog[open]")
            )
                return;
            const target = event.target as HTMLElement | null;
            const editing = target?.closest(
                "input,textarea,select,[contenteditable=true]",
            );
            const command =
                (event.ctrlKey || event.metaKey) &&
                !event.altKey &&
                event.key.toLowerCase() === "k";
            const slash =
                event.key === "/" &&
                !event.ctrlKey &&
                !event.metaKey &&
                !event.altKey;
            if (
                (!command && !slash) ||
                (editing && !(command && target === searchInput.current))
            )
                return;
            event.preventDefault();
            revealSearch(command);
        };
        window.addEventListener("keydown", shortcut);
        return () => {
            clearTimeout(searchTimer.current);
            window.removeEventListener("keydown", shortcut);
        };
    }, [authenticated, isPublic, searchLabel, revealSearch]);
    useEffect(() => {
        listingRefresh.current?.request();
    }, [refresh]);
    useEffect(() => {
        if (config.recovery_only || (!authenticated && !isPublic) || !isFiles) {
            loadedListing.current = null;
            setLoading(false);
            return;
        }
        const controller = new AbortController();
        // Keep the current table mounted when uploads update the same listing.
        let retryTimer: ReturnType<typeof setTimeout> | undefined;
        let loadingTimer: ReturnType<typeof setTimeout> | undefined;
        const retryNotice =
            "Could not update your files. Retrying automatically.";
        const queue = createListingRefreshQueue(async () => {
            clearTimeout(retryTimer);
            clearTimeout(loadingTimer);
            const background = loadedListing.current === listingKey;
            if (!background) {
                loadedListing.current = null;
                // Delay loading indicator by 180ms to eliminate flashes on fast page changes.
                loadingTimer = setTimeout(() => {
                    setLoading(true);
                }, 180);
            } else {
                setLoading(false);
            }
            setLoadError("");
            if (isPaged) {
                pageSource.current = background ? pageSource.current : null;
                const base = {
                    parent_id: isPublic
                        ? "/" +
                          (currentPath
                              .slice(7)
                              .split("/")
                              .filter(Boolean)
                              .at(-1) || "")
                        : currentPath,
                    sort: folderSort === "modified" ? "date" : folderSort,
                    order: folderOrder,
                    type: folderFilter === "all" ? undefined : folderFilter,
                } satisfies Omit<NodeQuery, "limit">;
                // shortcut: Recent ignores offset and around (it has neither);
                // its pages are only ever reached by continuing the cursor.
                const fetchPage: FetchPage = (request) =>
                    isRecent
                        ? api.recent(
                              {
                                  type: base.type,
                                  cursor: request.cursor,
                                  limit: request.cursor ? 100 : 50,
                              },
                              controller.signal,
                          )
                        : isPublic
                          ? api.sharedNodes(
                                { ...base, ...request },
                                location.auth,
                                controller.signal,
                            )
                          : api.nodes(
                                { ...base, ...request },
                                controller.signal,
                            );
                const loadFolder = async () => {
                    let next = await loadPrefix(
                        background ? pagesRef.current : emptyFolderPages,
                        fetchPage,
                        isRecent ? 50 : undefined,
                    );
                    const target = location.highlightId;
                    let wanted = background ? folderPage.current : 0;
                    if (
                        !background &&
                        target &&
                        !next.rows.some((item) => item?.id === target)
                    ) {
                        // "Show in folder" for an item beyond the first rows.
                        const found = await fetchPage({
                            around: target,
                            limit: 1,
                        }).catch(() => null);
                        if (found?.index !== undefined)
                            wanted = Math.floor(found.index / 100);
                    }
                    const request = pageRequest(next, wanted);
                    if (request)
                        next = applyPage(
                            next,
                            request,
                            await fetchPage(request),
                        );
                    return next;
                };
                return await loadFolder()
                    .then((next) => {
                        if (controller.signal.aborted) return false;
                        loadedListing.current = listingKey;
                        setToast((current) =>
                            current?.message === retryNotice ? null : current,
                        );
                        const merged = reconcilePages(pagesRef.current, next);
                        pagesRef.current = merged;
                        setPages(merged);
                        setPagesKey(listingKey);
                        pageSource.current = {
                            fetch: fetchPage,
                            pending: new Set(),
                        };
                        return true;
                    })
                    .catch((error) => failed(error, background))
                    .finally(() => {
                        clearTimeout(loadingTimer);
                        if (!controller.signal.aborted) setLoading(false);
                    });
            }
            const resultPromise =
                view === "starred"
                    ? api.starred(scopedQuery, controller.signal)
                    : view === "trash"
                      ? api.trash(scopedQuery, controller.signal)
                      : api
                            .search(
                                currentPath.slice(8),
                                searchOffset,
                                searchSort === "modified" ? "date" : searchSort,
                                controller.signal,
                                searchOrder,
                            )
                            .then((result) => {
                                if (!controller.signal.aborted) {
                                    setSearchMore(result.has_more);
                                    setSearchCapped(Boolean(result.capped));
                                    setNextSearchOffset(result.next_offset);
                                    setSearchItems((previous) =>
                                        reconcileDriveItems(
                                            previous,
                                            result.items,
                                        ),
                                    );
                                    if (
                                        !result.items.length &&
                                        searchOffset > 0
                                    )
                                        setSearchOffset(0);
                                }
                                return {
                                    data: {
                                        name: "Search Results",
                                        contents: Object.fromEntries(
                                            result.items.map((item) => [
                                                item.id,
                                                item,
                                            ]),
                                        ),
                                    } as Directory,
                                    auth_home_path: undefined,
                                };
                            });
            return await resultPromise
                .then((result) => {
                    if (controller.signal.aborted) return false;
                    loadedListing.current = listingKey;
                    setToast((current) =>
                        current?.message === retryNotice ? null : current,
                    );
                    setDirectory((previous) =>
                        reconcileDirectory(previous, result.data),
                    );
                    return true;
                })
                .catch((error) => failed(error, background))
                .finally(() => {
                    clearTimeout(loadingTimer);
                    if (!controller.signal.aborted) setLoading(false);
                });
        });
        const failed = (error: ApiError, background: boolean) => {
            if (controller.signal.aborted) return false;
            if (error.name !== "AbortError") {
                if (
                    background &&
                    (!error.status ||
                        error.status === 408 ||
                        error.status === 429 ||
                        error.status >= 500)
                ) {
                    notify(retryNotice, true);
                    retryTimer = setTimeout(reload, 3000);
                    return false;
                }
                loadedListing.current = null;
                if (isSearch) {
                    setSearchItems([]);
                    setSearchMore(false);
                    setSearchCapped(false);
                    setNextSearchOffset(null);
                }
                setLoadError(
                    isPublic && (error.status === 401 || error.status === 403)
                        ? "This link has expired, was revoked, or is not valid. Ask the owner for a new link."
                        : errorMessage(error),
                );
                setDirectory({ contents: {} });
                pagesRef.current = emptyFolderPages;
                setPages(emptyFolderPages);
            }
            return false;
        };
        listingRefresh.current = queue;
        queue.request();
        return () => {
            clearTimeout(retryTimer);
            clearTimeout(loadingTimer);
            controller.abort();
            pageSource.current = null;
            queue.dispose();
            if (listingRefresh.current === queue) listingRefresh.current = null;
        };
    }, [
        authenticated,
        isPublic,
        isFiles,
        isSearch,
        currentPath,
        location.auth,
        searchOffset,
        searchSort,
        searchOrder,
        scopedQuery,
        listingKey,
        notify,
        config.recovery_only,
        reload,
        isPaged,
        folderSort,
        folderOrder,
        folderFilter,
    ]);
    const ensurePage = useCallback(
        (page: number) => {
            folderPage.current = page;
            const source = pageSource.current;
            const request = source && pageRequest(pagesRef.current, page);
            if (!source || !request) return;
            const key = JSON.stringify(request);
            if (source.pending.has(key)) return;
            source.pending.add(key);
            void source
                .fetch(request)
                .then((result) => {
                    if (pageSource.current === source)
                        setPages((current) =>
                            applyPage(current, request, result),
                        );
                })
                .catch((error) => {
                    if (pageSource.current === source)
                        notify(errorMessage(error), true);
                })
                .finally(() => source.pending.delete(key));
        },
        [notify],
    );
    const highlightLookup = useRef("");
    useEffect(() => {
        // "Show in folder" while this folder is already open: no reload runs, so
        // find the item's page here when it is beyond the loaded rows.
        const target = location.highlightId;
        const source = pageSource.current;
        const lookup = listingKey + target;
        if (
            !target ||
            loading ||
            !isPaged ||
            !source ||
            highlightLookup.current === lookup ||
            pages.rows.some((item) => item?.id === target)
        )
            return;
        highlightLookup.current = lookup;
        void source
            .fetch({ around: target, limit: 1 })
            .then((found) => {
                if (
                    highlightLookup.current === lookup &&
                    found.index !== undefined
                )
                    ensurePage(Math.floor(found.index / 100));
            })
            .catch(() => {});
    }, [location.highlightId, listingKey, loading, isPaged, pages, ensurePage]);
    const previewLookup = useRef("");
    useEffect(() => {
        if (!isFiles || isSearch || view === "trash" || view === "starred")
            return;
        // The server returns the folder's breadcrumbs with its listing, so the
        // path never needs one request per ancestor, at any depth.
        const trail = directory.breadcrumbs || [];
        if (isPublic)
            setCrumbs(
                trail.map((crumb, index) => ({
                    path: "/share_" + crumb.id,
                    name: index ? crumb.name : "Shared Files",
                })),
            );
        else
            setCrumbs([
                ROOT_CRUMB,
                ...trail.slice(1).map((crumb) => ({
                    path: "/" + crumb.id,
                    name: crumb.name,
                })),
            ]);
    }, [directory.breadcrumbs, isPublic, isFiles, isSearch, view]);
    useEffect(() => {
        if (!location.previewId) previewLookup.current = "";
        if (!location.previewId || loading || view !== "files") return;
        const item = directory.contents[location.previewId];
        if (item?.type === "file") setDialog({ type: "preview", item });
        const source = pageSource.current;
        const lookup = listingKey + location.previewId;
        if (item || !isPaged || !source || previewLookup.current === lookup)
            return;
        // A preview link to an item beyond the loaded rows.
        previewLookup.current = lookup;
        void source
            .fetch({ around: location.previewId, limit: 1 })
            .then((page) => {
                const found = page.items[0];
                // A refresh replaces the page source, so match the request instead.
                if (
                    previewLookup.current === lookup &&
                    found?.id === location.previewId &&
                    found.type === "file"
                )
                    setDialog({ type: "preview", item: found });
            })
            .catch(() => {});
    }, [
        location.previewId,
        directory.contents,
        loading,
        view,
        isPaged,
        listingKey,
    ]);
    const closeDialog = () => {
        if (dialog?.type !== "preview") {
            if (
                dialog &&
                !["url", "torrent", "prepare", "archive", "extract"].includes(
                    dialog.type,
                )
            )
                reloadMetadata();
            setDialog(null);
            return;
        }
        setDialog(null);
        if (window.history.state?.tgdrivePreview) {
            window.history.back();
        } else {
            const url = new URL(window.location.href);
            url.searchParams.delete("preview");
            window.history.replaceState({}, "", url.pathname + url.search);
            setLocation(getLocation());
        }
    };
    const title =
        view === "starred"
            ? "Starred"
            : isRecent
              ? "Recent"
              : view === "preloads"
                ? "Preloads"
                : view === "performance"
                  ? "Performance"
                  : view === "trash"
                    ? "Trash"
                    : view === "activity"
                      ? "Transfers"
                      : view === "shares"
                        ? "Shared Links"
                        : view === "connections"
                          ? "Connections"
                          : view === "storage"
                            ? "Storage & Backups"
                            : view === "settings"
                              ? ADMIN_SETTING_TITLES[
                                    location.settingsSection
                                ] || "Settings"
                              : isSearch
                                ? "Search Results"
                                : isPublic
                                  ? directory.name && directory.name !== "/"
                                      ? directory.name
                                      : "Shared Files"
                                  : currentPath === "/"
                                    ? "My Files"
                                    : directory.name || "Folder";
    useEffect(() => {
        document.title =
            (config.recovery_only ? "Storage recovery" : title) + " · TGDrive";
    }, [title, config.recovery_only]);
    const destination = isSearch || view === "trash" ? "/" : currentPath;
    const addFiles = (files: File[]) => {
        if (!authenticated || isPublic) return;
        if (admissionPaused) {
            notify(
                "Restart TGDrive to apply the saved connection changes before uploading.",
                true,
            );
            return;
        }
        const rejected = transfers.add(
            files,
            destination,
            config.max_file_size || Number.MAX_SAFE_INTEGER,
        );
        if (rejected.length) notify(rejected.join("; "), true);
        setNewOpen(false);
    };
    const pickFiles = () => {
        setNewOpen(false);
        if (admissionPaused) {
            notify(
                "Restart TGDrive to apply the saved connection changes before uploading.",
                true,
            );
            return;
        }
        if (destination !== currentPath)
            notify("Files will be uploaded to My Files.");
        fileInput.current?.click();
    };
    const pickFolder = () => {
        if (!authenticated || isPublic) return;
        if (admissionPaused) {
            notify(
                "Restart TGDrive to apply the saved connection changes before uploading.",
                true,
            );
            return;
        }
        setNewOpen(false);
        folderDestination.current = {
            path: destination,
            name:
                destination === currentPath && view === "files" && !isSearch
                    ? title
                    : "My Files",
        };
        if (!folderSupported) {
            setFolderSelection({
                files: [],
                destination,
                name: folderDestination.current.name,
            });
            return;
        }
        folderInput.current?.click();
    };
    const locateTransfer = async (item: Transfer, folder = false) => {
        if (item.state !== "completed") return;
        try {
            const destination = folder
                ? transferFolderDestination(item)
                : item.destination;
            const target = await api.transferLocation(
                folder ? undefined : item.jobId,
                destination,
                !folder &&
                    !["torrent", "extract"].includes(item.kind || "") &&
                    item.state === "completed",
            );
            if (!target.available || !target.folder_path) {
                notify(
                    target.reason || "Location is no longer available",
                    true,
                );
                return;
            }
            let parent = target.folder_path;
            let highlight = target.item_id || "";
            if (folder && parent !== "/") {
                highlight = parent.split("/").pop() || "";
                parent = parent.slice(0, parent.lastIndexOf("/")) || "/";
            }
            navigate(parent, "files", "", false, {
                tgdriveHighlight: highlight,
                tgdriveReveal: true,
            });
        } catch (cause) {
            notify(errorMessage(cause), true);
        }
    };
    const open = (item: DriveItem) => {
        if (item.type === "folder") {
            navigate(
                (isPublic ? "/share_" : "/") + item.id,
                "files",
                location.auth,
            );
        } else {
            const url = new URL(window.location.href);
            url.searchParams.set("preview", item.id);
            window.history.pushState(
                { ...window.history.state, tgdrivePreview: true },
                "",
                url.pathname + url.search,
            );
            setLocation(getLocation());
            setDialog({ type: "preview", item });
        }
    };
    const action = async (
        type: "trash" | "restore" | "delete",
        targets: DriveItem[],
        allowPublic = false,
    ) => {
        try {
            const response = await api.bulk(
                targets.map(itemPath),
                type,
                allowPublic,
            );
            if (type === "delete") {
                const accepted = new Set(
                    (response.results || [])
                        .filter((result) => result.id)
                        .map((result) => result.path),
                );
                const next = removeRows(pagesRef.current, (item) =>
                    accepted.has(itemPath(item)),
                );
                pagesRef.current = next;
                setPages(next);
                setDirectory((current) => ({
                    ...current,
                    contents: Object.fromEntries(
                        Object.entries(current.contents).filter(
                            ([, item]) => !accepted.has(itemPath(item)),
                        ),
                    ),
                }));
                const names = new Map(
                    targets.map((item) => [itemPath(item), item.name]),
                );
                transfers.trackDeletions(
                    (response.results || []).flatMap((result) =>
                        result.id
                            ? [
                                  {
                                      id: result.id,
                                      name: names.get(result.path) || "Item",
                                  },
                              ]
                            : [],
                    ),
                );
            }
            const failed =
                response.results?.filter(
                    (result) => result.error || result.status === "error",
                ) || [];
            if (failed.length)
                throw new Error(
                    failed.length +
                        " item(s) could not be processed. " +
                        (failed[0].error || "Refresh and try again."),
                );
            notify(
                type === "trash"
                    ? "Moved to trash. Public access has been removed."
                    : type === "restore"
                      ? "Restored to your drive."
                      : "Deletion started.",
            );
            reloadMetadata();
        } catch (error) {
            if (
                type === "restore" &&
                error instanceof ApiError &&
                error.code.includes("PUBLIC_EXPOSURE")
            )
                setDialog({ type: "restore", items: targets });
            else if (type !== "delete" && !allowPublic)
                notify(errorMessage(error), true);
            reloadMetadata();
            throw error;
        }
    };
    const clearSearch = () => {
        clearTimeout(searchTimer.current);
        setQuery("");
        setScopedQuery("");
        setSearchOffset(0);
        if (view === "files" && isSearch) {
            const origin = searchOrigin.current || lastFolder.current;
            searchOrigin.current = null;
            ownSearchNavigation.current = null;
            navigate(origin, "files", "", true);
        }
        searchInput.current?.focus();
    };
    const applySearch = (raw: string, commit = false) => {
        const term = raw.trim().normalize("NFC");
        if (!term) {
            clearSearch();
            return;
        }
        if ([...term].length > 255) {
            notify("Search names with at most 255 characters.", true);
            return;
        }
        if (view !== "files") {
            setScopedQuery(term);
            return;
        }
        const path = "/search_" + term;
        if (path === currentPath && view === "files") return;
        if (!isSearch)
            searchOrigin.current =
                view === "files" ? currentPath : lastFolder.current;
        const origin = searchOrigin.current || "/";
        ownSearchNavigation.current = path;
        setSearchOffset(0);
        navigate(path, "files", "", isSearch && !commit, {
            tgdriveSearchOrigin: origin,
        });
    };
    const changeSearch = (value: string) => {
        setQuery(value);
        clearTimeout(searchTimer.current);
        if (searchComposing.current) return;
        if (!value.trim()) {
            clearSearch();
            return;
        }
        searchTimer.current = setTimeout(() => applySearch(value), 300);
    };
    const onSearch = (event: FormEvent) => {
        event.preventDefault();
        if (searchComposing.current) return;
        clearTimeout(searchTimer.current);
        applySearch(query, true);
    };
    const navigationLink = (item: {
        view: View;
        label: string;
        icon: typeof HardDrive;
        path?: string;
        section?: string;
    }) => {
        const params = new URLSearchParams();
        if (item.view === "trash") params.set("path", "/trash");
        else if (item.view !== "files") params.set("view", item.view);
        if (item.section) params.set("section", item.section);
        const current =
            view === item.view &&
            (item.view !== "settings" ||
                location.settingsSection === (item.section || "cache"));
        return (
            <a
                key={item.label}
                href={"/" + (params.size ? "?" + params.toString() : "")}
                aria-current={current ? "page" : undefined}
                onClick={(event) => {
                    event.preventDefault();
                    navigate(
                        item.path || "/",
                        item.view,
                        "",
                        false,
                        {},
                        item.section,
                    );
                }}
            >
                <item.icon size={19} aria-hidden="true" />
                <span>{item.label}</span>
                {item.view === "activity" && activeCount > 0 && (
                    <span className="nav-count">{activeCount}</span>
                )}
            </a>
        );
    };
    const pageHeading = (
        <div className="page-heading">
            <div>
                {isFiles &&
                    !isSearch &&
                    view !== "trash" &&
                    view !== "starred" &&
                    !isRecent &&
                    crumbs.length > 1 && (
                        <nav className="breadcrumbs" aria-label="Breadcrumb">
                            {crumbs.slice(0, -1).map((crumb, index, shown) =>
                                // Deep paths show the first and last two
                                // folders; the button reveals the rest.
                                !crumbsExpanded &&
                                shown.length > 5 &&
                                index > 0 &&
                                index < shown.length - 2 ? (
                                    index === 1 && (
                                        <span key="collapsed">
                                            <ChevronRight
                                                size={14}
                                                aria-hidden="true"
                                            />
                                            <button
                                                type="button"
                                                className="text-button"
                                                aria-label={
                                                    "Show " +
                                                    (shown.length - 3) +
                                                    " hidden folders"
                                                }
                                                onClick={() =>
                                                    setCrumbsExpanded(true)
                                                }
                                            >
                                                …
                                            </button>
                                        </span>
                                    )
                                ) : (
                                    <span key={crumb.path}>
                                        {index > 0 && (
                                            <ChevronRight
                                                size={14}
                                                aria-hidden="true"
                                            />
                                        )}
                                        <a
                                            href={
                                                "/?" +
                                                new URLSearchParams({
                                                    path: crumb.path,
                                                    ...(location.auth
                                                        ? {
                                                              auth: location.auth,
                                                          }
                                                        : {}),
                                                })
                                            }
                                            onClick={(event) => {
                                                event.preventDefault();
                                                navigate(
                                                    crumb.path,
                                                    "files",
                                                    location.auth,
                                                );
                                            }}
                                            aria-current={
                                                index === crumbs.length - 1
                                                    ? "page"
                                                    : undefined
                                            }
                                        >
                                            {crumb.name}
                                        </a>
                                    </span>
                                ),
                            )}
                        </nav>
                    )}
                <div className="page-title-line">
                    <h1>{title}</h1>
                    {config.demo_data && (
                        <span
                            className="demo-marker"
                            title="Synthetic local preview content and metrics"
                        >
                            Demo
                        </span>
                    )}
                </div>
                {isSearch && (
                    <p className="page-subtitle">
                        Files and folders matching “{currentPath.slice(8)}”
                        across your drive
                    </p>
                )}
                {view === "trash" && (
                    <p className="page-subtitle">
                        Trashed items cannot be opened through public links.
                    </p>
                )}
            </div>
            {!isPublic && searchLabel && mobile && (
                <button
                    ref={searchTrigger}
                    type="button"
                    className="icon-button compact-search-trigger"
                    aria-label={searchLabel}
                    aria-controls="page-search-form"
                    aria-expanded={searchExpanded || Boolean(query)}
                    onClick={() => revealSearch()}
                >
                    <Search size={19} aria-hidden="true" />
                </button>
            )}
            {!isPublic && searchLabel && (
                <form
                    id="page-search-form"
                    hidden={mobile && !searchExpanded && !query}
                    className={
                        "search-form" +
                        (isSearch && loading ? " search-pending" : "")
                    }
                    role="search"
                    aria-label={searchLabel}
                    onSubmit={onSearch}
                    onBlur={(event) => {
                        if (
                            mobile &&
                            !query &&
                            !event.currentTarget.contains(event.relatedTarget)
                        )
                            setSearchExpanded(false);
                    }}
                    aria-busy={isSearch && loading}
                >
                    <button
                        type="submit"
                        className="icon-button"
                        aria-label="Search"
                    >
                        <Search size={18} aria-hidden="true" />
                    </button>
                    <label className="sr-only" htmlFor="file-search">
                        {searchLabel}
                    </label>
                    <input
                        id="file-search"
                        ref={searchInput}
                        type="search"
                        maxLength={255}
                        aria-describedby="search-help"
                        value={query}
                        onFocus={() => setSearchExpanded(true)}
                        onChange={(event) => changeSearch(event.target.value)}
                        onCompositionStart={() => {
                            searchComposing.current = true;
                            clearTimeout(searchTimer.current);
                        }}
                        onCompositionEnd={(event) => {
                            searchComposing.current = false;
                            changeSearch(event.currentTarget.value);
                        }}
                        onKeyDown={(event) => {
                            if (
                                event.key === "Escape" &&
                                !searchComposing.current
                            ) {
                                event.preventDefault();
                                clearSearch();
                                if (mobile) {
                                    requestedSearchFocus.current = null;
                                    setSearchExpanded(false);
                                    searchTrigger.current?.focus();
                                }
                            }
                        }}
                        placeholder={searchLabel}
                    />
                    <span id="search-help" className="sr-only">
                        {view === "files"
                            ? "Search names across your drive. Press Escape to return to the previous folder."
                            : "Search this page. Press Escape to clear the search."}
                    </span>
                    {mobile && !query && (
                        <button
                            className="icon-button"
                            type="button"
                            aria-label="Close Search"
                            onClick={() => {
                                requestedSearchFocus.current = null;
                                setSearchExpanded(false);
                                searchTrigger.current?.focus();
                            }}
                        >
                            <X size={17} aria-hidden="true" />
                        </button>
                    )}
                    {query && (
                        <button
                            className="icon-button"
                            type="button"
                            aria-label="Clear Search"
                            onClick={clearSearch}
                        >
                            <X size={17} />
                        </button>
                    )}
                </form>
            )}
            {!mobile && <ThemeControl />}
        </div>
    );
    const configGate = (
        <div
            className="deployment-config-state settings-section"
            role={configLoad === "error" ? "alert" : "status"}
            aria-busy={configLoad === "loading"}
        >
            {configLoad === "error" ? (
                <>
                    <p>
                        Configuration and management are unavailable until
                        TGDrive’s configuration can be loaded. You can continue
                        using file features.
                    </p>
                    <p className="settings-help">{configError}</p>
                    <button
                        type="button"
                        className="secondary-button"
                        onClick={() => void loadConfig()}
                    >
                        Try Again
                    </button>
                </>
            ) : (
                <div
                    className="config-skeleton"
                    role="status"
                    aria-label="Loading configuration"
                >
                    <span className="sr-only">Loading configuration…</span>
                    <span
                        className="skeleton-block"
                        style={{ height: "80px", borderRadius: "10px" }}
                    />
                </div>
            )}
        </div>
    );
    if (authenticated === null && !isPublic)
        return (
            <div className="startup" role="status">
                <BrandLogo size="large" />
                <span className="startup-status">
                    <span className="startup-pulse" />
                    <span className="sr-only">Opening TGDrive…</span>
                </span>
            </div>
        );
    if (authenticated === false && !isPublic)
        return (
            <>
                <Login
                    onSuccess={() => {
                        setConfigLoad("loading");
                        setConfigReady(false);
                        void loadConfig();
                        setAuthenticated(true);
                        reload();
                        setToast(null);
                    }}
                />
                {toast && <Toast toast={toast} close={() => setToast(null)} />}
            </>
        );
    if (config.recovery_only && !isPublic)
        return (
            <div className="channel-recovery-shell">
                <header className="channel-recovery-topbar">
                    <a href="/" className="brand">
                        <BrandLogo />
                    </a>
                    <div>
                        <ThemeControl />
                        <SidebarLogout onLoggedOut={endSession} />
                    </div>
                </header>
                <main id="main-content" className="channel-recovery-main">
                    <h1>Storage recovery</h1>
                    <p className="channel-recovery-intro">
                        File changes are paused. Restore a backup below, or
                        start a new drive if you do not need the old one.
                    </p>
                    {configLoad === "ready" ? (
                        <>
                            <ChannelBackup
                                recoveryOnly
                                demoMode={demoReadOnly}
                                onNotice={notify}
                            />
                            <StartNewDrive demoMode={demoReadOnly} />
                        </>
                    ) : (
                        configGate
                    )}
                </main>
                {toast && <Toast toast={toast} close={() => setToast(null)} />}
            </div>
        );
    return (
        <div className={"app-shell " + (isPublic ? "public-shell" : "")}>
            <a className="skip-link" href="#main-content">
                Skip to Files
            </a>
            {!isPublic && (
                <aside
                    id="drive-navigation"
                    role={mobile && mobileNav ? "dialog" : undefined}
                    aria-modal={mobile && mobileNav ? true : undefined}
                    className={"sidebar " + (mobileNav ? "nav-open" : "")}
                    aria-label="Drive navigation"
                    inert={mobile && !mobileNav}
                >
                    <a
                        className="brand"
                        href="/"
                        onClick={(event) => {
                            event.preventDefault();
                            navigate("/");
                        }}
                    >
                        <BrandLogo />
                    </a>
                    {mobile && (
                        <div className="mobile-nav-heading">
                            <span className="mobile-drawer-brand">
                                <BrandLogo />
                            </span>
                            <button
                                className="icon-button"
                                aria-label="Close Navigation"
                                onClick={() => setMobileNav(false)}
                            >
                                <X size={20} aria-hidden="true" />
                            </button>
                        </div>
                    )}
                    <button
                        id="new-button"
                        disabled={admissionPaused}
                        className="new-button"
                        aria-expanded={newOpen}
                        aria-controls="new-menu"
                        onClick={() => {
                            if (view !== "files") {
                                navigate("/", "files");
                                setNewOpen(true);
                            } else setNewOpen(!newOpen);
                        }}
                    >
                        <Plus size={23} /> New
                    </button>
                    <nav className="drive-nav">
                        {navigationLink({
                            view: "files",
                            label: "My Files",
                            icon: FolderClosed,
                        })}
                        {navigationLink({
                            view: "recent",
                            label: "Recent",
                            icon: Clock,
                        })}
                        {navigationLink({
                            view: "starred",
                            label: "Starred",
                            icon: Star,
                        })}
                        {navigationLink({
                            view: "activity",
                            label: "Transfers",
                            icon: ArrowDownUp,
                        })}
                        {navigationLink({
                            view: "shares",
                            label: "Shared Links",
                            icon: Link,
                        })}
                        {navigationLink({
                            view: "trash",
                            label: "Trash",
                            icon: Trash2,
                            path: "/trash",
                        })}
                        <details
                            className="sidebar-tools"
                            open={toolsOpen}
                            onToggle={(event) =>
                                setToolsOpen(event.currentTarget.open)
                            }
                        >
                            <summary
                                id="tools-settings-summary"
                                aria-controls="tools-settings-content"
                            >
                                <SlidersHorizontal
                                    size={19}
                                    aria-hidden="true"
                                />
                                <span>Tools & settings</span>
                                <ChevronRight
                                    className="nav-disclosure"
                                    size={16}
                                    aria-hidden="true"
                                />
                            </summary>
                            <div
                                className="sidebar-tools-content"
                                id="tools-settings-content"
                                role="group"
                                aria-labelledby="tools-settings-summary"
                            >
                                <div
                                    className="monitor-nav"
                                    role="group"
                                    aria-label="Monitor"
                                >
                                    <span className="nav-section-label">
                                        Monitor
                                    </span>
                                    {navigationLink({
                                        view: "performance",
                                        label: "Performance",
                                        icon: ChartNoAxesCombined,
                                    })}
                                    {navigationLink({
                                        view: "preloads",
                                        label: "Preloads",
                                        icon: Server,
                                    })}
                                </div>
                                <div
                                    className="admin-nav"
                                    role="group"
                                    aria-label="Configuration"
                                >
                                    <span className="nav-section-label">
                                        Configuration
                                    </span>
                                    {navigationLink({
                                        view: "settings",
                                        section: "cache",
                                        label: "Cache settings",
                                        icon: Database,
                                    })}
                                    {navigationLink({
                                        view: "settings",
                                        section: "transfers",
                                        label: "Transfer limits",
                                        icon: Gauge,
                                    })}
                                    {navigationLink({
                                        view: "settings",
                                        section: "regional",
                                        label: "Timezone",
                                        icon: Globe,
                                    })}
                                </div>
                                <div
                                    className="admin-nav"
                                    role="group"
                                    aria-label="Management"
                                >
                                    <span className="nav-section-label">
                                        Management
                                    </span>
                                    {navigationLink({
                                        view: "connections",
                                        label: "Connections",
                                        icon: Plug,
                                    })}
                                    {navigationLink({
                                        view: "storage",
                                        label: "Storage & Backups",
                                        icon: ShieldCheck,
                                    })}
                                </div>
                            </div>
                        </details>
                    </nav>
                    <div className="sidebar-bottom">
                        {config.demo_data && (
                            <small className="demo-data-label">
                                Local demo · sample content & metrics
                            </small>
                        )}
                        <div className="sidebar-support">
                            <a
                                className="sidebar-help"
                                href="https://github.com/TechShreyash/TGDrive/blob/main/docs/README.md"
                                target="_blank"
                                rel="noopener noreferrer"
                            >
                                <BookOpen size={18} aria-hidden="true" />{" "}
                                Documentation
                            </a>
                            <a
                                className="sidebar-project"
                                href="https://github.com/TechShreyash/TGDrive"
                                target="_blank"
                                rel="noopener noreferrer"
                                aria-label="TGDrive on GitHub"
                                title="View TGDrive on GitHub"
                            >
                                <GitHubMark />
                            </a>
                        </div>
                        <SidebarLogout
                            onLoggedOut={() => {
                                setMobileNav(false);
                                setNewOpen(false);
                                endSession();
                                notify(
                                    "Logged out. Server transfers continue.",
                                );
                            }}
                        />
                    </div>
                </aside>
            )}
            {mobile && mobileNav && (
                <button
                    className="nav-dismiss"
                    tabIndex={-1}
                    aria-label="Close Navigation"
                    onClick={() => setMobileNav(false)}
                />
            )}
            <main
                id="main-content"
                className="main-content"
                tabIndex={-1}
                inert={mobile && mobileNav}
                onDragOver={(event) => {
                    if (event.dataTransfer.types.includes("Files"))
                        event.preventDefault();
                }}
                onDrop={(event) => {
                    if (!event.dataTransfer.files.length) return;
                    event.preventDefault();
                    if (isPublic) return;
                    if (
                        view !== "files" ||
                        isSearch ||
                        dialog ||
                        folderSelection
                    ) {
                        notify(
                            "Open a folder in My Files before dropping files.",
                            true,
                        );
                        return;
                    }
                    if (
                        Array.from(event.dataTransfer.items).some(
                            (item) => item.webkitGetAsEntry?.()?.isDirectory,
                        )
                    ) {
                        notify(
                            "Use New → Upload Folder to keep the folder structure.",
                            true,
                        );
                        return;
                    }
                    addFiles(Array.from(event.dataTransfer.files));
                }}
            >
                <header className="topbar" hidden={!mobile && !isPublic}>
                    {isPublic ? (
                        <a href="/" className="brand">
                            <BrandLogo />
                        </a>
                    ) : (
                        <>
                            <button
                                id="navigation-toggle"
                                className="icon-button mobile-menu"
                                aria-controls="drive-navigation"
                                aria-label="Toggle Navigation"
                                aria-expanded={mobileNav}
                                onClick={() => setMobileNav(!mobileNav)}
                            >
                                <Menu size={22} />
                            </button>
                        </>
                    )}
                    {mobile && !isPublic && (
                        <>
                            <a
                                className="mobile-brand"
                                href="/"
                                onClick={(event) => {
                                    event.preventDefault();
                                    navigate("/");
                                }}
                            >
                                <BrandLogo size="compact" />
                            </a>
                            {view === "files" && !isSearch && !dialog && (
                                <button
                                    id="compact-new-button"
                                    disabled={admissionPaused}
                                    className="mobile-new primary-button"
                                    onClick={() => setNewOpen(!newOpen)}
                                    aria-expanded={newOpen}
                                    aria-controls="new-menu"
                                >
                                    <Plus size={18} /> New
                                </button>
                            )}
                        </>
                    )}
                    {mobile && <ThemeControl />}
                    {isPublic && (
                        <span className="public-label">
                            <Globe size={16} /> Read-Only Shared Folder
                        </span>
                    )}
                </header>
                <section
                    ref={workspace}
                    className={"workspace" + (isFiles ? " file-workspace" : "")}
                    aria-label={title}
                >
                    {!isPublic && admissionPaused && (
                        <div className="restart-required-banner" role="status">
                            <ShieldAlert size={20} />
                            <div>
                                <strong>File changes are paused</strong>
                                <p>
                                    File changes and new uploads are paused. You
                                    can still browse, preview and download.
                                </p>
                            </div>
                            {view !== "connections" && (
                                <button
                                    type="button"
                                    className="text-button"
                                    onClick={() => navigate("/", "connections")}
                                >
                                    Open Connections
                                </button>
                            )}
                        </div>
                    )}
                    {!isPublic && telegramWarning && (
                        <div className="restart-required-banner" role="status">
                            <ShieldAlert size={20} />
                            <div>
                                <strong>
                                    A Telegram connection needs attention
                                </strong>
                                <p>{telegramWarning}</p>
                            </div>
                            {view !== "connections" && (
                                <button
                                    type="button"
                                    className="text-button"
                                    onClick={() => navigate("/", "connections")}
                                >
                                    Open Connections
                                </button>
                            )}
                        </div>
                    )}
                    {!isFiles && pageHeading}
                    {!isPublic && view === "trash" && (
                        <DeletionNotice
                            jobs={deletionProgress.jobs}
                            completed={deletionProgress.completed}
                            progressError={deletionProgress.error}
                        />
                    )}
                    {isFiles ? (
                        <FileBrowser
                            header={pageHeading}
                            searchQuery={
                                ["trash", "starred"].includes(view)
                                    ? scopedQuery
                                    : ""
                            }
                            directory={directory}
                            listingKey={listingKey}
                            shareRefreshKey={JSON.stringify([
                                view,
                                isSearch ? "search" : currentPath,
                                metadataRefresh,
                            ])}
                            highlightId={location.highlightId}
                            loading={loading}
                            refreshing={listingRefreshAction.refreshing}
                            error={loadError}
                            isPublic={isPublic}
                            isTrash={view === "trash"}
                            isStarred={view === "starred"}
                            isRecent={isRecent}
                            onLocate={
                                isRecent
                                    ? (item) =>
                                          navigate(
                                              item.path,
                                              "files",
                                              "",
                                              false,
                                              {
                                                  tgdriveHighlight: item.id,
                                                  tgdriveReveal: true,
                                              },
                                          )
                                    : undefined
                            }
                            onStar={(item) => {
                                void api
                                    .star(itemPath(item), !item.starred)
                                    .then(() => {
                                        notify(
                                            item.starred
                                                ? "Removed from Starred."
                                                : "Added to Starred.",
                                        );
                                        reload();
                                    })
                                    .catch((error) =>
                                        notify(errorMessage(error), true),
                                    );
                            }}
                            isSearch={isSearch}
                            auth={location.auth}
                            onReload={() =>
                                void listingRefreshAction.run(
                                    async () => {
                                        invalidateMetadata();
                                        return (
                                            listingRefresh.current?.request() ??
                                            false
                                        );
                                    },
                                    view === "trash"
                                        ? "Trash refreshed."
                                        : view === "starred"
                                          ? "Starred files refreshed."
                                          : "Files refreshed.",
                                )
                            }
                            onUpload={pickFiles}
                            onOpen={open}
                            onDialog={setDialog}
                            onAction={(type, targets) =>
                                void action(type, targets).catch(() => {})
                            }
                            searchPagination={
                                isSearch
                                    ? {
                                          offset: searchOffset,
                                          hasMore: searchMore,
                                          capped: searchCapped,
                                          onPrevious: () =>
                                              setSearchOffset(
                                                  Math.max(
                                                      0,
                                                      searchOffset - 100,
                                                  ),
                                              ),
                                          onNext: () => {
                                              if (nextSearchOffset !== null)
                                                  setSearchOffset(
                                                      nextSearchOffset,
                                                  );
                                          },
                                      }
                                    : undefined
                            }
                            searchItems={isSearch ? searchItems : undefined}
                            paging={
                                isPaged
                                    ? {
                                          rows: pages.rows,
                                          total: pages.total,
                                          complete:
                                              pagesKey === listingKey &&
                                              folderComplete(pages),
                                          folderKey,
                                          // Recent has one fixed order, newest activity first.
                                          sort: isRecent
                                              ? "modified"
                                              : folderSort,
                                          order: isRecent
                                              ? "desc"
                                              : folderOrder,
                                          filter: folderFilter,
                                          onSort: (sort, order) => {
                                              if (isRecent) return;
                                              setFolderSort(sort);
                                              setFolderOrder(order);
                                          },
                                          onFilter: (filter) =>
                                              setFolderFilterState({
                                                  path: folderKey,
                                                  filter,
                                              }),
                                          onPage: ensurePage,
                                      }
                                    : undefined
                            }
                            searchSort={isSearch ? searchSort : undefined}
                            searchOrder={isSearch ? searchOrder : undefined}
                            onSearchOrder={(value) => {
                                setSearchOrder(value);
                                setSearchOffset(0);
                            }}
                            onSearchSort={(value) => {
                                setSearchSort(value);
                                setSearchOffset(0);
                            }}
                        />
                    ) : view === "shares" ? (
                        <SharesView
                            query={scopedQuery}
                            refresh={refresh}
                            onError={(message) => notify(message, true)}
                            onNotice={notify}
                            onChange={reloadMetadata}
                            onLocate={(share) =>
                                navigate(
                                    share.folder_path ||
                                        share.path.slice(
                                            0,
                                            share.path.lastIndexOf("/"),
                                        ) ||
                                        "/",
                                    "files",
                                    "",
                                    false,
                                    {
                                        tgdriveHighlight:
                                            share.node_id ||
                                            share.path.split("/").pop(),
                                        tgdriveReveal: true,
                                    },
                                )
                            }
                        />
                    ) : view === "preloads" ? (
                        <Preloads
                            query={scopedQuery}
                            onError={(message) => notify(message, true)}
                            onNotice={notify}
                        />
                    ) : view === "performance" ? (
                        <PerformancePanel onNotice={notify} />
                    ) : ["connections", "storage", "settings"].includes(
                          view,
                      ) ? (
                        !configReady ? (
                            configGate
                        ) : (
                            <>
                                {configLoad !== "ready" && configGate}
                                <fieldset
                                    style={{ display: "contents" }}
                                    disabled={configLoad !== "ready"}
                                    inert={configLoad !== "ready"}
                                >
                                    {view === "connections" ? (
                                        <SettingsManagement
                                            demoMode={demoReadOnly}
                                            onNotice={notify}
                                        />
                                    ) : view === "storage" ? (
                                        <StorageView
                                            config={config}
                                            demoMode={demoReadOnly}
                                            onNotice={notify}
                                            onTransfers={() =>
                                                navigate("/", "activity")
                                            }
                                            onTrash={() =>
                                                navigate("/trash", "trash")
                                            }
                                        />
                                    ) : view === "settings" ? (
                                        <PerformanceSettings
                                            demoMode={demoReadOnly}
                                            onNotice={notify}
                                            category={
                                                location.settingsSection ===
                                                "transfers"
                                                    ? "transfers"
                                                    : location.settingsSection ===
                                                        "regional"
                                                      ? "regional"
                                                      : "cache"
                                            }
                                            uploadPreferences={
                                                <BotModeSettings
                                                    demoMode={demoReadOnly}
                                                    onNotice={notify}
                                                />
                                            }
                                            onSaved={(value) => {
                                                setTimezone(value.timezone);
                                                void loadConfig();
                                                notify(
                                                    "Performance settings saved.",
                                                );
                                                reload();
                                            }}
                                        />
                                    ) : null}
                                </fieldset>
                            </>
                        )
                    ) : (
                        <TransferHistory
                            onNotice={notify}
                            query={scopedQuery}
                            items={items}
                            onLocate={(item, folder) =>
                                void locateTransfer(item, folder)
                            }
                            onError={(message) => notify(message, true)}
                        />
                    )}
                </section>
                {mobile &&
                    !isPublic &&
                    view !== "activity" &&
                    (mobileActiveCount > 0 || mobileUploadIssues) && (
                        <button
                            className="mobile-transfer-status"
                            onClick={() => navigate("/", "activity")}
                            aria-label="Open transfer progress"
                        >
                            <Upload size={18} aria-hidden="true" />
                            <span>
                                {mobileActiveCount > 0
                                    ? mobileActiveCount +
                                      (mobileActiveCount === 1
                                          ? " upload in progress"
                                          : " uploads in progress")
                                    : "Uploads need attention"}
                            </span>
                            <ChevronRight size={18} aria-hidden="true" />
                        </button>
                    )}
                {mobile && !isPublic && (
                    <nav className="mobile-tabbar" aria-label="Sections">
                        {[
                            {
                                view: "files" as View,
                                label: "Files",
                                icon: FolderClosed,
                            },
                            {
                                view: "starred" as View,
                                label: "Starred",
                                icon: Star,
                            },
                            {
                                view: "activity" as View,
                                label: "Transfers",
                                icon: ArrowDownUp,
                            },
                        ].map((tab) => (
                            <button
                                key={tab.view}
                                type="button"
                                aria-current={
                                    view === tab.view ? "page" : undefined
                                }
                                onClick={() => navigate("/", tab.view)}
                            >
                                <span className="mobile-tab-icon">
                                    <tab.icon size={21} aria-hidden="true" />
                                    {tab.view === "activity" &&
                                        activeCount > 0 && (
                                            <span className="mobile-tab-count">
                                                {activeCount}
                                            </span>
                                        )}
                                </span>
                                {tab.label}
                            </button>
                        ))}
                        <button
                            type="button"
                            aria-current={
                                !["files", "starred", "activity"].includes(view)
                                    ? "page"
                                    : undefined
                            }
                            aria-controls="drive-navigation"
                            aria-expanded={mobileNav}
                            onClick={() => setMobileNav(true)}
                        >
                            <span className="mobile-tab-icon">
                                <Menu size={21} aria-hidden="true" />
                            </span>
                            More
                        </button>
                    </nav>
                )}
            </main>
            {!mobile &&
                !isPublic &&
                ["files", "trash", "shares"].includes(view) && (
                    <TransferPanel
                        pageContext={view}
                        items={items}
                        onLocate={(item, folder) =>
                            void locateTransfer(item, folder)
                        }
                        onError={(message) => notify(message, true)}
                    />
                )}
            {newOpen && (
                <>
                    <button
                        className="menu-dismiss"
                        aria-label="Close New Item Menu"
                        onClick={() => setNewOpen(false)}
                    />
                    <div
                        id="new-menu"
                        className="new-menu"
                        role="group"
                        aria-label="New Item Actions"
                    >
                        <button id="file-upload-action" onClick={pickFiles}>
                            <Upload size={18} /> Upload Files
                        </button>
                        <button id="folder-upload-action" onClick={pickFolder}>
                            <FolderUp size={18} /> Upload Folder
                        </button>
                        <button
                            onClick={() => {
                                setDialog({ type: "folder" });
                                setNewOpen(false);
                            }}
                        >
                            <FolderPlus size={18} /> New Folder
                        </button>
                        <button
                            onClick={() => {
                                setDialog({ type: "url" });
                                setNewOpen(false);
                            }}
                        >
                            <Globe size={18} /> Upload from URL
                        </button>
                        <button
                            onClick={() => {
                                setDialog({ type: "torrent" });
                                setNewOpen(false);
                            }}
                        >
                            <Magnet size={18} /> Import torrent
                        </button>
                    </div>
                </>
            )}
            <input
                id="file-input"
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                    addFiles(Array.from(event.target.files || []));
                    event.target.value = "";
                }}
            />
            <input
                id="folder-input"
                ref={(node) => {
                    folderInput.current = node;
                    if (node) node.webkitdirectory = true;
                }}
                type="file"
                multiple
                hidden
                aria-label="Choose Folder to Upload"
                onChange={(event) => {
                    const selected = Array.from(event.target.files || []);
                    event.target.value = "";
                    setFolderSelection({
                        files: selected,
                        destination: folderDestination.current.path,
                        name: folderDestination.current.name,
                    });
                }}
            />
            {folderSelection && !isPublic && authenticated && (
                <FolderUpload
                    files={folderSelection.files}
                    destination={folderSelection.destination}
                    destinationName={folderSelection.name}
                    maxSize={config.max_file_size || Number.MAX_SAFE_INTEGER}
                    supported={folderSupported}
                    onClose={() => setFolderSelection(null)}
                    onChanged={reload}
                    onFilesFallback={pickFiles}
                    onQueued={(root, count) => {
                        setFolderSelection(null);
                        notify(
                            count.toLocaleString() +
                                " files queued in “" +
                                root.name +
                                "”. Track each file in Transfers.",
                        );
                    }}
                />
            )}
            {dialog && (
                <ActionDialog
                    key={dialog.type + ("item" in dialog ? dialog.item.id : "")}
                    dialog={dialog}
                    destination={destination}
                    auth={location.auth}
                    onClose={closeDialog}
                    onChanged={() => {
                        reloadMetadata();
                        if (dialog.type !== "download-link") setDialog(null);
                    }}
                    onNotice={notify}
                    onDelete={(targets) => action("delete", targets)}
                    onRestore={(targets) => action("restore", targets, true)}
                />
            )}
            {demoFilesReadOnly && (
                <Modal
                    title="Demo Files is read-only"
                    initialFocus="#demo-files-dismiss"
                    onClose={() => setDemoFilesReadOnly(false)}
                >
                    <p>
                        Demo Files and its contents are read-only for demo
                        visitors. You can open, preview and download them. Open
                        My Files to upload files, create folders and try changes
                        outside Demo Files.
                    </p>
                    <div className="dialog-actions">
                        <button
                            id="demo-files-dismiss"
                            type="button"
                            className="secondary-button"
                            onClick={() => setDemoFilesReadOnly(false)}
                        >
                            Close
                        </button>
                        <button
                            type="button"
                            className="primary-button"
                            onClick={() => {
                                setDemoFilesReadOnly(false);
                                navigate("/");
                            }}
                        >
                            Open My Files
                        </button>
                    </div>
                </Modal>
            )}
            {toast && <Toast toast={toast} close={() => setToast(null)} />}
        </div>
    );
}
