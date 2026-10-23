import BrandMark from "./BrandMark";
import GitHubMark from "./GitHubMark";
import ThemeControl from "./ThemeControl";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";
import {
  Star,
  FolderClosed,
  ArrowDownUp,
  ChartNoAxesCombined,
  Layers,
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
  ShieldCheck,
  ShieldAlert,
  Menu,
  SlidersHorizontal,
} from "lucide-react";
import { api, ApiError, errorMessage } from "./api";
import {
  itemPath,
  formatBytes,
  setTimezone,
  type DriveItem,
  type Directory,
  type View,
} from "./types";
import { transfers, type Transfer } from "./transfers";
import { Spinner, ToastNotice as Toast } from "./components";
import { Login, SharesView, StorageView, UploadSettings } from "./Screens";
import SettingsManagement from "./SettingsManagement";
import PerformanceSettings from "./PerformanceSettings";
import Preloads from "./Preloads";
import PerformancePanel from "./PerformancePanel";
import FileBrowser from "./FileBrowser";
import type { FileSort } from "./SortMenu";
import type { SortOrder } from "./fileSorting";
import ActionDialog, { type ActionDialogState } from "./ActionDialog";
import TransferPanel from "./TransferPanel";
import TransferHistory from "./TransferHistory";
import FolderUpload from "./FolderUpload";
import DeletionNotice from "./DeletionNotice";
import SidebarLogout from "./SidebarLogout";
import ChannelBackup from "./ChannelBackup";
import BotModeSettings from "./BotModeSettings";
function getLocation() {
  const url = new URL(window.location.href);
  let path = url.searchParams.get("path") || "/";
  let requested = url.searchParams.get("view");
  const section = url.searchParams.get("section");
  const validSection = [
    "cache",
    "transfers",
    "regional",
    "performance",
  ].includes(section || "");
  if (
    requested === "settings" &&
    !validSection &&
    !path.startsWith("/share_")
  ) {
    url.searchParams.delete("view");
    url.searchParams.delete("section");
    url.searchParams.delete("path");
    window.history.replaceState(
      window.history.state,
      "",
      url.pathname + url.search + url.hash,
    );
    requested = null;
    path = "/";
  }
  const view: View = path.startsWith("/share_")
    ? "files"
    : path === "/trash"
      ? "trash"
      : [
            "activity",
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
    settingsSection:
      url.searchParams.get("section") === "performance"
        ? "cache"
        : ["cache", "transfers", "regional"].includes(
              url.searchParams.get("section") || "",
            )
          ? url.searchParams.get("section")!
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
    recovery_only?: boolean;
  }>({ max_file_size: 0 });
  const [directory, setDirectory] = useState<Directory>({ contents: {} });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [dialog, setDialog] = useState<ActionDialogState | null>(null);
  const [query, setQuery] = useState("");
  const [scopedQuery, setScopedQuery] = useState("");
  const [searchOffset, setSearchOffset] = useState(searchHistoryOffset);
  const [searchSort, setSearchSort] = useState<FileSort>("name");
  const [searchOrder, setSearchOrder] = useState<SortOrder>("asc");
  const [searchMore, setSearchMore] = useState(false);
  const [searchItems, setSearchItems] = useState<DriveItem[]>([]);
  const [nextSearchOffset, setNextSearchOffset] = useState<number | null>(null);
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
        menu?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ||
          [],
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
      if (event.key === "ArrowDown") next = (current + 1) % actions.length;
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
    () => window.matchMedia("(max-width: 720px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 720px)");
    const update = () => {
      setMobile(media.matches);
      if (!media.matches) setMobileNav(false);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const [crumbs, setCrumbs] = useState([ROOT_CRUMB]);
  const [shareRoot, setShareRoot] = useState("");
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
  const names = useRef(new Map([["/", "My Files"]]));
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
  const isFiles = view === "files" || view === "trash" || view === "starred";
  const listingKey = JSON.stringify([
    authenticated,
    view,
    currentPath,
    location.auth,
    isSearch ? searchOffset : 0,
    isSearch ? searchSort : "",
    isSearch ? searchOrder : "",
    ["trash", "starred"].includes(view) ? scopedQuery : "",
  ]);
  const loadedListing = useRef<string | null>(null);
  const workspace = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (workspace.current) workspace.current.scrollTop = 0;
  }, [currentPath, view, location.settingsSection, searchOffset]);
  const items = useSyncExternalStore(transfers.subscribe, transfers.snapshot);
  const deletionJobs = useSyncExternalStore(
    transfers.subscribe,
    transfers.deletionSnapshot,
  );
  const activeCount = items.filter(
    (item) => !["completed", "error", "cancelled"].includes(item.state),
  ).length;
  const mobileUploadIssues = items.some(
    (item) =>
      item.initiatedHere && ["error", "awaiting_file"].includes(item.state),
  );
  const notify = useCallback(
    (message: string, error = false) => setToast({ message, error }),
    [],
  );
  const reload = useCallback(() => setRefresh((value) => value + 1), []);
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
      if (path !== "/") params.set("path", path);
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
    void api
      .config()
      .then((value) => {
        setConfig(value);
        setTimezone(value.timezone || "Asia/Kolkata");
      })
      .catch(() => {});
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
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refreshAdmission = async () => {
      try {
        const status = await api.status();
        if (!disposed) {
          setAdmissionPaused(
            Boolean(status.admission_paused || status.restart_required),
          );
          setTelegramWarning(
            status.telegram_issues?.[0]?.detail ||
              (status.telegram_ready === false &&
              !status.demo &&
              !status.recovery_only
                ? "No usable Telegram account is connected. Open Connections and Refresh to check bot tokens, Premium sessions and channel permissions. Existing files and saved upload progress are preserved."
                : ""),
          );
        }
      } catch {
        // Keep the last known admission state until the status service recovers.
      } finally {
        if (!disposed) timer = setTimeout(refreshAdmission, 5000);
      }
    };
    void refreshAdmission();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [authenticated, isPublic]);
  useEffect(() => {
    const saved = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          admission_paused?: boolean;
          restart_required?: boolean;
        }>
      ).detail;
      setAdmissionPaused(
        Boolean(detail?.admission_paused || detail?.restart_required),
      );
      setNewOpen(false);
    };
    const required = () => {
      setAdmissionPaused(true);
      setNewOpen(false);
    };
    window.addEventListener("tgdrive:settings-saved", saved);
    window.addEventListener("tgdrive:restart-required", required);
    return () => {
      window.removeEventListener("tgdrive:settings-saved", saved);
      window.removeEventListener("tgdrive:restart-required", required);
    };
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
      setAuthenticated(false);
      notify("Your session expired. Log in to continue.", true);
    };
    window.addEventListener("tgdrive:auth-required", expired);
    window.addEventListener("tgdrive:files-changed", reload);
    return () => {
      window.removeEventListener("tgdrive:auth-required", expired);
      window.removeEventListener("tgdrive:files-changed", reload);
    };
  }, [notify, reload]);
  useEffect(() => {
    if (!toast || toast.error) return;
    const timer = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(timer);
  }, [toast]);
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
        event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey;
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
    if (config.recovery_only || (!authenticated && !isPublic) || !isFiles) {
      loadedListing.current = null;
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    // Keep the current table mounted when uploads update the same listing.
    const background = loadedListing.current === listingKey;
    if (!background) loadedListing.current = null;
    setLoading(!background);
    setLoadError("");
    const resultPromise =
      view === "starred"
        ? api.starred(scopedQuery, controller.signal)
        : view === "trash"
          ? api.trash(scopedQuery, controller.signal)
          : isSearch && !isPublic
            ? api
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
                    setNextSearchOffset(result.next_offset);
                    setSearchItems(result.items);
                    if (!result.items.length && searchOffset > 0)
                      setSearchOffset(0);
                  }
                  return {
                    data: {
                      name: "Search Results",
                      contents: Object.fromEntries(
                        result.items.map((item) => [item.id, item]),
                      ),
                    } as Directory,
                    auth_home_path: undefined,
                  };
                })
            : api.directory(currentPath, location.auth, controller.signal);
    void resultPromise
      .then((result) => {
        if (controller.signal.aborted) return;
        loadedListing.current = listingKey;
        setDirectory(result.data);
        setShareRoot(result.auth_home_path || "");
        if (result.data.name) names.current.set(currentPath, result.data.name);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error.name !== "AbortError") {
          if (
            background &&
            (!error.status ||
              error.status === 408 ||
              error.status === 429 ||
              error.status >= 500)
          ) {
            notify("Could not update your files. " + errorMessage(error), true);
            return;
          }
          loadedListing.current = null;
          if (isSearch) {
            setSearchItems([]);
            setSearchMore(false);
            setNextSearchOffset(null);
          }
          setLoadError(
            isPublic && (error.status === 401 || error.status === 403)
              ? "This link has expired, was revoked, or is not valid. Ask the owner for a new link."
              : errorMessage(error),
          );
          setDirectory({ contents: {} });
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    authenticated,
    isPublic,
    isFiles,
    isSearch,
    currentPath,
    location.auth,
    refresh,
    searchOffset,
    searchSort,
    searchOrder,
    scopedQuery,
    listingKey,
    notify,
    config.recovery_only,
  ]);
  useEffect(() => {
    if (
      config.recovery_only ||
      !isFiles ||
      isSearch ||
      view === "trash" ||
      view === "starred"
    )
      return;
    let cancelled = false;
    const realPath = isPublic
      ? "/" + currentPath.slice(7).replace(/^\/+/, "")
      : currentPath;
    const segments = realPath.split("/").filter(Boolean);
    let start = 0;
    const base = [ROOT_CRUMB];
    if (isPublic) {
      const root = shareRoot || realPath;
      start = root.split("/").filter(Boolean).length;
      base[0] = {
        path: "/share_" + root.replace(/^\/+/, ""),
        name: "Shared Files",
      };
    }
    const paths = segments
      .map((_, index) => "/" + segments.slice(0, index + 1).join("/"))
      .slice(start);
    void Promise.all(
      paths.map(async (path) => {
        const navPath = isPublic ? "/share_" + path.slice(1) : path;
        let name = names.current.get(navPath);
        if (!name) {
          try {
            const result = await api.directory(navPath, location.auth);
            name = result.data.name || "Folder";
            names.current.set(navPath, name);
          } catch {
            name = "Folder";
          }
        }
        return { path: navPath, name };
      }),
    ).then((result) => {
      if (!cancelled) setCrumbs([...base, ...result]);
    });
    return () => {
      cancelled = true;
    };
  }, [
    currentPath,
    isPublic,
    isSearch,
    isFiles,
    view,
    shareRoot,
    config.recovery_only,
    location.auth,
    directory.name,
  ]);
  useEffect(() => {
    if (!location.previewId || loading || view !== "files") return;
    const item = directory.contents[location.previewId];
    if (item?.type === "file") setDialog({ type: "preview", item });
  }, [location.previewId, directory.contents, loading, view]);
  const closeDialog = () => {
    if (dialog?.type !== "preview") {
      if (dialog?.type === "share") reload();
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
                      ? ADMIN_SETTING_TITLES[location.settingsSection] ||
                        "Settings"
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
      let destination = item.destination;
      if (folder && item.relativePath) {
        const depth = item.relativePath.split("/").length - 2;
        for (let index = 0; index < depth; index++)
          destination = destination.replace(/\/?[^/]+$/, "") || "/";
      }
      const target = await api.transferLocation(
        folder ? undefined : item.jobId,
        destination,
        !folder && item.state === "completed",
      );
      if (!target.available || !target.folder_path) {
        notify(target.reason || "Location is no longer available", true);
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
    if (item.type === "folder")
      navigate(
        isPublic
          ? "/share_" + itemPath(item).replace(/^\/+/, "")
          : itemPath(item),
        "files",
        location.auth,
      );
    else {
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
      const response = await api.bulk(targets.map(itemPath), type, allowPublic);
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
      if (type === "delete") transfers.refreshJobs();
      notify(
        type === "trash"
          ? "Moved to trash. Public access has been removed."
          : type === "restore"
            ? "Restored to your drive."
            : "Deletion started.",
      );
      reload();
    } catch (error) {
      if (
        type === "restore" &&
        error instanceof ApiError &&
        error.code.includes("PUBLIC_EXPOSURE")
      )
        setDialog({ type: "restore", items: targets });
      else notify(errorMessage(error), true);
      reload();
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
          navigate(item.path || "/", item.view, "", false, {}, item.section);
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
          crumbs.length > 1 && (
            <nav className="breadcrumbs" aria-label="Breadcrumb">
              {crumbs.slice(0, -1).map((crumb, index) => (
                <span key={crumb.path}>
                  {index > 0 && <ChevronRight size={14} aria-hidden="true" />}
                  <a
                    href={
                      "/?" +
                      new URLSearchParams({
                        path: crumb.path,
                        ...(location.auth ? { auth: location.auth } : {}),
                      })
                    }
                    onClick={(event) => {
                      event.preventDefault();
                      navigate(crumb.path, "files", location.auth);
                    }}
                    aria-current={
                      index === crumbs.length - 1 ? "page" : undefined
                    }
                  >
                    {crumb.name}
                  </a>
                </span>
              ))}
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
            Files and folders matching “{currentPath.slice(8)}” across your
            drive
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
            "search-form" + (isSearch && loading ? " search-pending" : "")
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
          <button type="submit" className="icon-button" aria-label="Search">
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
              if (event.key === "Escape" && !searchComposing.current) {
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
  if (authenticated === null && !isPublic)
    return (
      <div className="startup" role="status">
        <Spinner />
        <span>Opening TGDrive…</span>
      </div>
    );
  if (authenticated === false && !isPublic)
    return (
      <>
        <Login
          onSuccess={() => {
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
            <BrandMark size={28} />
            <strong>TGDrive</strong>
          </a>
          <div>
            <ThemeControl />
            <SidebarLogout onLoggedOut={() => setAuthenticated(false)} />
          </div>
        </header>
        <main id="main-content" className="channel-recovery-main">
          <h1>Storage recovery</h1>
          <p className="channel-recovery-intro">
            Your local drive is unavailable. File changes are paused while you
            recover it.
          </p>
          <ChannelBackup recoveryOnly />
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
            <span className="brand-symbol">
              <BrandMark size={30} />
            </span>
            <strong>TGDrive</strong>
          </a>
          {mobile && (
            <div className="mobile-nav-heading">
              <span className="mobile-drawer-brand">
                <BrandMark size={28} />
                <strong>TGDrive</strong>
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
            {navigationLink({ view: "starred", label: "Starred", icon: Star })}
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
              onToggle={(event) => setToolsOpen(event.currentTarget.open)}
            >
              <summary
                id="tools-settings-summary"
                aria-controls="tools-settings-content"
              >
                <SlidersHorizontal size={19} aria-hidden="true" />
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
                <div className="monitor-nav" role="group" aria-label="Monitor">
                  <span className="nav-section-label">Monitor</span>
                  {navigationLink({
                    view: "performance",
                    label: "Performance",
                    icon: ChartNoAxesCombined,
                  })}
                  {navigationLink({
                    view: "preloads",
                    label: "Preloads",
                    icon: Layers,
                  })}
                </div>
                <div
                  className="admin-nav"
                  role="group"
                  aria-label="Configuration"
                >
                  <span className="nav-section-label">Configuration</span>
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
                <div className="admin-nav" role="group" aria-label="Management">
                  <span className="nav-section-label">Management</span>
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
                href="https://github.com/TechShreyash/TGDrive/blob/dev/docs/README.md"
                target="_blank"
                rel="noopener noreferrer"
              >
                <BookOpen size={18} aria-hidden="true" /> Documentation
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
                setAuthenticated(false);
                notify("Logged out. Server transfers continue.");
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
          if (view !== "files" || isSearch || dialog || folderSelection) {
            notify("Open a folder in My Files before dropping files.", true);
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
              <BrandMark size={28} />
              <strong>TGDrive</strong>
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
                <BrandMark size={24} /> TGDrive
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
                <strong>Restart TGDrive to apply connection changes</strong>
                <p>
                  File changes and new uploads are paused. You can still browse,
                  preview and download.
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
                <strong>A Telegram connection needs attention</strong>
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
          {!isPublic && <DeletionNotice jobs={deletionJobs} />}
          {isFiles ? (
            <FileBrowser
              header={pageHeading}
              searchQuery={
                ["trash", "starred"].includes(view) ? scopedQuery : ""
              }
              directory={directory}
              listingKey={listingKey}
              highlightId={location.highlightId}
              loading={loading}
              error={loadError}
              isPublic={isPublic}
              isTrash={view === "trash"}
              isStarred={view === "starred"}
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
                  .catch((error) => notify(errorMessage(error), true));
              }}
              isSearch={isSearch}
              auth={location.auth}
              onReload={reload}
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
                      onPrevious: () =>
                        setSearchOffset(Math.max(0, searchOffset - 100)),
                      onNext: () => {
                        if (nextSearchOffset !== null)
                          setSearchOffset(nextSearchOffset);
                      },
                    }
                  : undefined
              }
              searchItems={isSearch ? searchItems : undefined}
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
              onChange={reload}
              onLocate={(share) =>
                navigate(
                  share.folder_path ||
                    share.path.slice(0, share.path.lastIndexOf("/")) ||
                    "/",
                  "files",
                  "",
                  false,
                  {
                    tgdriveHighlight:
                      share.node_id || share.path.split("/").pop(),
                    tgdriveReveal: true,
                  },
                )
              }
              onShare={(item) => setDialog({ type: "share", item })}
            />
          ) : view === "preloads" ? (
            <Preloads
              query={scopedQuery}
              onError={(message) => notify(message, true)}
              onNotice={notify}
            />
          ) : view === "performance" ? (
            <PerformancePanel />
          ) : view === "connections" ? (
            <SettingsManagement />
          ) : view === "storage" ? (
            <StorageView
              config={config}
              onTransfers={() => navigate("/", "activity")}
              onTrash={() => navigate("/trash", "trash")}
            />
          ) : view === "settings" ? (
            <PerformanceSettings
              category={
                location.settingsSection === "transfers"
                  ? "transfers"
                  : location.settingsSection === "regional"
                    ? "regional"
                    : "cache"
              }
              uploadPreferences={
                <>
                  <UploadSettings />
                  <BotModeSettings />
                </>
              }
              onSaved={(value) => {
                setTimezone(value.timezone);
                void api
                  .config()
                  .then(setConfig)
                  .catch(() => {});
                notify("Performance settings saved.");
                reload();
              }}
            />
          ) : (
            <TransferHistory
              query={scopedQuery}
              items={items}
              onLocate={(item, folder) => void locateTransfer(item, folder)}
              onError={(message) => notify(message, true)}
            />
          )}
        </section>
        {mobile &&
          !isPublic &&
          view !== "activity" &&
          (activeCount > 0 || mobileUploadIssues) && (
            <button
              className="mobile-transfer-status"
              onClick={() => navigate("/", "activity")}
              aria-label="Open transfer progress"
            >
              <Upload size={18} aria-hidden="true" />
              <span>
                {activeCount > 0
                  ? activeCount +
                    (activeCount === 1
                      ? " upload in progress"
                      : " uploads in progress")
                  : "Uploads need attention"}
              </span>
              <ChevronRight size={18} aria-hidden="true" />
            </button>
          )}
      </main>
      {!mobile && !isPublic && ["files", "trash", "shares"].includes(view) && (
        <TransferPanel
          pageContext={view}
          items={items}
          onLocate={(item, folder) => void locateTransfer(item, folder)}
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
            reload();
            if (dialog.type !== "download-link") setDialog(null);
          }}
          onNotice={notify}
          onDelete={(targets) => action("delete", targets)}
          onRestore={(targets) => action("restore", targets, true)}
        />
      )}
      {toast && <Toast toast={toast} close={() => setToast(null)} />}
    </div>
  );
}
