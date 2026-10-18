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
  FolderPlus,
  FolderUp,
  HardDrive,
  Search,
  Trash2,
  Upload,
  Plus,
  Link,
  ChevronRight,
  X,
  Settings,
  Plug,
  Activity,
  Globe,
  ShieldCheck,
  ShieldAlert,
  Menu,
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
import { Login, SharesView, SettingsView, StorageView } from "./Screens";
import SettingsManagement from "./SettingsManagement";
import PerformanceSettings from "./PerformanceSettings";
import Preloads from "./Preloads";
import PerformancePanel from "./PerformancePanel";
import FileBrowser from "./FileBrowser";
import type { FileSort } from "./SortMenu";
import ActionDialog, { type ActionDialogState } from "./ActionDialog";
import TransferPanel from "./TransferPanel";
import TransferHistory from "./TransferHistory";
import FolderUpload from "./FolderUpload";
import DeletionNotice from "./DeletionNotice";
import ThemeControl from "./ThemeControl";
function getLocation() {
  const url = new URL(window.location.href);
  const path = url.searchParams.get("path") || "/";
  const requested = url.searchParams.get("view");
  const view: View = path.startsWith("/share_")
    ? "files"
    : path === "/trash"
      ? "trash"
      : [
            "activity",
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
    previewId: url.searchParams.get("preview") || "",
    highlightId:
      typeof window.history.state?.tgdriveHighlight === "string"
        ? window.history.state.tgdriveHighlight
        : "",
  };
}
const ROOT_CRUMB = { path: "/", name: "My Files" };
function searchHistoryOffset() {
  const value = window.history.state?.tgdriveSearchOffset;
  return Number.isSafeInteger(value) && value >= 0 && value % 100 === 0
    ? value
    : 0;
}
export default function App() {
  const [location, setLocation] = useState(getLocation);
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [admissionPaused, setAdmissionPaused] = useState(false);
  const [config, setConfig] = useState({ max_file_size: 0 });
  const [directory, setDirectory] = useState<Directory>({ contents: {} });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [dialog, setDialog] = useState<ActionDialogState | null>(null);
  const [query, setQuery] = useState("");
  const [searchOffset, setSearchOffset] = useState(searchHistoryOffset);
  const [searchSort, setSearchSort] = useState<FileSort>("name");
  const [searchMore, setSearchMore] = useState(false);
  const [searchItems, setSearchItems] = useState<DriveItem[]>([]);
  const [nextSearchOffset, setNextSearchOffset] = useState<number | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
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
    const controls = () =>
      Array.from(
        navigation?.querySelectorAll<HTMLElement>(
          "a[href],button:not(:disabled)",
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
      opener?.focus({ preventScroll: true });
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
  const isFiles = view === "files" || view === "trash";
  const workspace = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (workspace.current) workspace.current.scrollTop = 0;
  }, [currentPath, view, searchOffset]);
  const items = useSyncExternalStore(transfers.subscribe, transfers.snapshot);
  const deletionJobs = useSyncExternalStore(
    transfers.subscribe,
    transfers.deletionSnapshot,
  );
  const activeCount = items.filter(
    (item) => !["completed", "error", "cancelled"].includes(item.state),
  ).length;
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
    ) => {
      clearTimeout(searchTimer.current);
      const params = new URLSearchParams();
      if (path !== "/") params.set("path", path);
      if (view !== "files" && view !== "trash") params.set("view", view);
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
    transfers.setEnabled(authenticated === true && !isPublic);
    return () => transfers.setEnabled(false);
  }, [authenticated, isPublic]);
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
      searchInput.current?.focus();
      if (command) searchInput.current?.select();
    };
    window.addEventListener("keydown", shortcut);
    return () => {
      clearTimeout(searchTimer.current);
      window.removeEventListener("keydown", shortcut);
    };
  }, [authenticated, isPublic]);
  useEffect(() => {
    if ((!authenticated && !isPublic) || !isFiles) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    const resultPromise =
      isSearch && !isPublic
        ? api
            .search(
              currentPath.slice(8),
              searchOffset,
              searchSort === "modified" ? "date" : searchSort,
              controller.signal,
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
        setDirectory(result.data);
        setShareRoot(result.auth_home_path || "");
        if (result.data.name) names.current.set(currentPath, result.data.name);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error.name !== "AbortError") {
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
  ]);
  useEffect(() => {
    if (!isFiles || isSearch || view === "trash") return;
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
    view === "preloads"
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
                    ? "Settings"
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
    document.title = title + " · TGDrive";
  }, [title]);
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
    setSearchOffset(0);
    if (isSearch) {
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
        <div className="login-theme-control">
          <ThemeControl />
        </div>
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
              <HardDrive size={23} />
            </span>
            <strong>TGDrive</strong>
          </a>
          {mobile && (
            <div className="mobile-nav-heading">
              <strong>TGDrive</strong>
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
            {(
              [
                {
                  view: "files",
                  label: "My Files",
                  icon: HardDrive,
                  path: "/",
                },
                {
                  view: "activity",
                  label: "Transfers",
                  icon: Activity,
                  path: "/",
                },
                {
                  view: "shares",
                  label: "Shared Links",
                  icon: Link,
                  path: "/",
                },
                {
                  view: "preloads",
                  label: "Preloads",
                  icon: HardDrive,
                  path: "/",
                },
                {
                  view: "performance",
                  label: "Performance",
                  icon: Activity,
                  path: "/",
                },
                { view: "trash", label: "Trash", icon: Trash2, path: "/trash" },
                {
                  view: "connections",
                  label: "Connections",
                  icon: Plug,
                  path: "/",
                },
                {
                  view: "storage",
                  label: "Storage & Backups",
                  icon: ShieldCheck,
                  path: "/",
                },
                {
                  view: "settings",
                  label: "Settings",
                  icon: Settings,
                  path: "/",
                },
              ] as const
            ).map((item) => (
              <a
                key={item.view}
                data-section-start={
                  item.view === "connections" ? true : undefined
                }
                href={
                  "/?" +
                  new URLSearchParams(
                    item.view === "trash"
                      ? { path: "/trash" }
                      : item.view === "files"
                        ? {}
                        : { view: item.view },
                  ).toString()
                }
                aria-current={view === item.view ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(item.path, item.view);
                }}
              >
                <item.icon size={19} />
                <span>{item.label}</span>
                {item.view === "activity" && activeCount > 0 && (
                  <span className="nav-count">{activeCount}</span>
                )}
              </a>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <small>
              Uploads up to{" "}
              {config.max_file_size
                ? formatBytes(config.max_file_size)
                : "the server limit"}
            </small>
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
        <header className="topbar">
          {isPublic ? (
            <a href="/" className="brand">
              <HardDrive size={22} />
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
              <form
                className={
                  "search-form" + (isSearch && loading ? " search-pending" : "")
                }
                role="search"
                onSubmit={onSearch}
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
                  Search Files and Folders
                </label>
                <input
                  id="file-search"
                  ref={searchInput}
                  type="search"
                  maxLength={255}
                  aria-describedby="search-help"
                  value={query}
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
                    }
                  }}
                  placeholder="Search Files and Folders"
                />
                <span id="search-help" className="sr-only">
                  Search file and folder names across your drive. Press Escape
                  to return to the previous folder.
                </span>
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
            </>
          )}
          <ThemeControl />
          {isPublic && (
            <span className="public-label">
              <Globe size={16} /> Read-Only Shared Folder
            </span>
          )}
        </header>
        <section ref={workspace} className="workspace" aria-label={title}>
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
          <div className="page-heading">
            <div>
              {isFiles &&
                !isSearch &&
                view !== "trash" &&
                crumbs.length > 1 && (
                  <nav className="breadcrumbs" aria-label="Breadcrumb">
                    {crumbs.map((crumb, index) => (
                      <span key={crumb.path}>
                        {index > 0 && (
                          <ChevronRight size={14} aria-hidden="true" />
                        )}
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
              <h1>{title}</h1>
              {isSearch && (
                <p className="page-subtitle">
                  Files and folders matching “{currentPath.slice(8)}” across
                  your drive
                </p>
              )}
              {view === "trash" && (
                <p className="page-subtitle">
                  Trashed items cannot be opened through public links.
                </p>
              )}
            </div>
          </div>
          {!isPublic && <DeletionNotice jobs={deletionJobs} />}
          {isFiles ? (
            <FileBrowser
              directory={directory}
              highlightId={location.highlightId}
              loading={loading}
              error={loadError}
              isPublic={isPublic}
              isTrash={view === "trash"}
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
              onSearchSort={(value) => {
                setSearchSort(value);
                setSearchOffset(0);
              }}
            />
          ) : view === "shares" ? (
            <SharesView
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
            <>
              <SettingsView
                onLogout={async () => {
                  await api.logout();
                  setAuthenticated(false);
                  notify("Logged out. Server transfers continue.");
                }}
              />
              <PerformanceSettings
                onSaved={(value) => {
                  setTimezone(value.timezone);
                  notify("Performance settings saved.");
                  reload();
                }}
              />
            </>
          ) : (
            <TransferHistory
              items={items}
              onLocate={(item, folder) => void locateTransfer(item, folder)}
              onError={(message) => notify(message, true)}
            />
          )}
        </section>
      </main>
      {!isPublic && ["files", "trash", "shares"].includes(view) && (
        <TransferPanel
          pageContext={view}
          items={items}
          onLocate={(item, folder) => void locateTransfer(item, folder)}
          onError={(message) => notify(message, true)}
        />
      )}
      {!isPublic && view === "files" && !isSearch && !dialog && (
        <button
          id="compact-new-button"
          disabled={admissionPaused}
          className="mobile-new primary-button"
          onClick={() => {
            if (view !== "files") {
              navigate("/", "files");
              setNewOpen(true);
            } else setNewOpen(!newOpen);
          }}
          aria-expanded={newOpen}
          aria-controls="new-menu"
        >
          <Plus size={21} /> New
        </button>
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
