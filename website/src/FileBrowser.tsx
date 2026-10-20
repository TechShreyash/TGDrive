import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertCircle,
  Folder,
  Upload,
  Move,
  Trash2,
  RefreshCw,
  Download,
  ChevronRight,
  ChevronLeft,
  MoreHorizontal,
  Pencil,
  Eye,
  Link,
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
} from "./types";
import { api } from "./api";
import { shareExpired } from "./SharesView";
import { FileIcon, Spinner } from "./components";
import SortMenu, { type FileSort } from "./SortMenu";
import "./FileBrowser.css";

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
      Math.min(bounds.right - menu.width, window.innerWidth - menu.width - 12),
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
  highlightId = "",
  loading,
  error,
  isPublic,
  isTrash,
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
  searchItems,
  header,
  searchQuery = "",
}: {
  directory: Directory;
  listingKey: string;
  highlightId?: string;
  loading: boolean;
  error: string;
  isPublic: boolean;
  isTrash: boolean;
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
  }, [directory, isPublic, isTrash]);
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
      paths: new Set(active.map((share) => share.path.replace(/\/+$/, ""))),
    };
  }, [activeShares, shareTick]);
  const sharing = (item: DriveItem) => {
    let path = itemPath(item);
    if (shareIndex.ids.has(item.id) || shareIndex.paths.has(path))
      return "Shared by Link";
    while (path.includes("/")) {
      path = path.slice(0, path.lastIndexOf("/"));
      if (shareIndex.paths.has(path)) return "Shared Through Parent Folder";
    }
    return "";
  };
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectionMode, setSelectionMode] = useState(false);
  const [localSort, setLocalSort] = useState<FileSort>("name");
  const sort = searchSort || localSort;
  const setSort = (value: FileSort) => {
    if (searchPagination) onSearchSort?.(value);
    else setLocalSort(value);
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
      setSelected(new Set());
      setSelectionMode(false);
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, []);
  const allItems = useMemo(() => {
    const items = (
      searchItems || Object.values(directory.contents || {})
    ).slice();
    if (!searchPagination)
      items.sort((a, b) => {
        if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
        if (sort === "size") return (b.size || 0) - (a.size || 0);
        if (sort === "modified")
          return (
            (Date.parse(b.upload_date || "") || 0) -
            (Date.parse(a.upload_date || "") || 0)
          );
        return a.name.localeCompare(b.name, undefined, {
          numeric: true,
          sensitivity: "base",
        });
      });
    return items;
  }, [directory.contents, searchItems, searchPagination, sort]);
  useEffect(() => {
    // Retain browsing state on refresh, dropping only selections that disappeared.
    const ids = new Set(allItems.map((item) => item.id));
    setSelected((current) => {
      const next = new Set([...current].filter((id) => ids.has(id)));
      return next.size === current.size ? current : next;
    });
    setPage((current) =>
      Math.min(current, Math.max(0, Math.ceil(allItems.length / 100) - 1)),
    );
  }, [allItems]);
  useEffect(() => {
    document.querySelector(".file-list-scroll")?.scrollTo({ top: 0 });
  }, [page, listingKey]);
  const highlighted = useRef("");
  useEffect(() => {
    if (loading || !highlightId) return;
    const target = JSON.stringify([listingKey, highlightId, sort]);
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
        row
          ?.querySelector<HTMLButtonElement>(".item-name-button")
          ?.focus({ preventScroll: true });
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [allItems, listingKey, highlightId, loading, sort]);
  const visibleItems = allItems.slice(page * 100, (page + 1) * 100);
  const chosen = allItems.filter((item) => selected.has(item.id));
  const pointerKind = useRef("");
  const pendingTouch = useRef<{ id: string; at: number } | null>(null);
  const touchOrigin = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    pendingTouch.current = null;
  }, [listingKey]);
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
    additive = false,
    touch = false,
  ) => {
    if (isTrash && keyboard) return;
    if (keyboard) {
      pendingTouch.current = null;
      onOpen(item);
    } else {
      if (touch && !isTrash && !additive) {
        const now = performance.now();
        const previous = pendingTouch.current;
        pendingTouch.current = { id: item.id, at: now };
        if (previous?.id === item.id && now - previous.at <= 700) {
          pendingTouch.current = null;
          onOpen(item);
          return;
        }
      } else pendingTouch.current = null;
      if (!isPublic) setSelectionMode(true);
      setSelected((current) => {
        const next = additive ? new Set(current) : new Set<string>();
        if (additive && next.has(item.id)) next.delete(item.id);
        else next.add(item.id);
        return next;
      });
    }
  };
  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!(event.target as Element).closest(".item-actions")) hideItemMenus();
      if (!(event.target as Element).closest(".file-row"))
        pendingTouch.current = null;
    };
    document.addEventListener("click", close);
    window.addEventListener("resize", hideItemMenus);
    window.addEventListener("scroll", hideItemMenus, true);
    return () => {
      document.removeEventListener("click", close);
      window.removeEventListener("resize", hideItemMenus);
      window.removeEventListener("scroll", hideItemMenus, true);
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
  return (
    <div className="file-browser">
      <div className="file-toolbar">
        {header}
        <div className="file-toolbar-controls">
          {chosen.length && !isPublic ? (
            <>
              <span className="selection-count">{chosen.length} selected</span>
              <button
                className="text-button"
                onClick={() => {
                  setSelected(new Set());
                  setSelectionMode(false);
                }}
              >
                Done
              </button>
              <div className="toolbar-divider" />
              {isTrash ? (
                <>
                  <button
                    className="text-button"
                    onClick={() => onAction("restore", chosen)}
                  >
                    <RefreshCw size={16} /> Restore
                  </button>
                  <button
                    className="text-button danger-text"
                    onClick={() =>
                      showDialog({ type: "delete", items: chosen })
                    }
                  >
                    <Trash2 size={16} /> Delete Permanently
                  </button>
                </>
              ) : (
                <>
                  <button
                    className="text-button"
                    onClick={() => showDialog({ type: "move", items: chosen })}
                  >
                    <Move size={16} /> Move
                  </button>
                  <button
                    className="text-button"
                    onClick={() => onAction("trash", chosen)}
                  >
                    <Trash2 size={16} /> Trash
                  </button>
                </>
              )}
            </>
          ) : (
            <>
              <span>
                {searchPagination
                  ? allItems.length
                    ? "Showing " +
                      (searchPagination.offset + 1) +
                      "–" +
                      (searchPagination.offset + allItems.length) +
                      (searchPagination.hasMore ? " · More available" : "")
                    : "No Results"
                  : allItems.length +
                    (allItems.length === 1 ? " item" : " items")}
                {isPublic ? " · Read-only" : ""}
              </span>
              <div className="file-toolbar-actions">
                {!isPublic && (
                  <button
                    type="button"
                    className="text-button"
                    aria-pressed={selectionMode}
                    onClick={() => {
                      setSelectionMode(!selectionMode);
                      setSelected(new Set());
                    }}
                  >
                    {selectionMode ? "Done" : "Select"}
                  </button>
                )}
                <SortMenu
                  value={sort}
                  onChange={(value) => {
                    setSort(value);
                    setPage(0);
                  }}
                />
              </div>
            </>
          )}
        </div>
      </div>
      <div
        className="file-list-scroll"
        tabIndex={0}
        aria-label="File list"
        onPointerDownCapture={(event) => {
          touchOrigin.current =
            event.pointerType === "touch"
              ? { x: event.clientX, y: event.clientY }
              : null;
        }}
        onPointerMoveCapture={(event) => {
          const origin = touchOrigin.current;
          if (
            origin &&
            (Math.abs(event.clientX - origin.x) > 12 ||
              Math.abs(event.clientY - origin.y) > 12)
          ) {
            pendingTouch.current = null;
            touchOrigin.current = null;
          }
        }}
        onPointerUpCapture={() => {
          touchOrigin.current = null;
        }}
        onPointerCancelCapture={() => {
          pendingTouch.current = null;
          touchOrigin.current = null;
        }}
        onWheel={() => {
          pendingTouch.current = null;
        }}
        onKeyDownCapture={() => {
          pendingTouch.current = null;
        }}
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
            <button className="secondary-button" onClick={onReload}>
              Try Again
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
              {isTrash && searchQuery
                ? "No Matching Trashed Items"
                : isTrash
                  ? "Trash Is Empty"
                  : isSearch
                    ? "No Matching Files or Folders"
                    : "Nothing Here Yet"}
            </h2>
            <p>
              {isTrash && searchQuery
                ? "Try another name or clear the search. This search stays in Trash."
                : isTrash
                  ? "Items you trash stay here until you restore or permanently delete them."
                  : isSearch
                    ? "Try another file or folder name."
                    : isPublic
                      ? "The owner has not added any files to this folder."
                      : "Upload your first file or create a folder to get started."}
            </p>
            {!isPublic && !isTrash && !isSearch && (
              <button className="primary-button" onClick={onUpload}>
                <Upload size={17} /> Upload Files
              </button>
            )}
          </div>
        ) : (
          <>
            <div className="file-table-wrap">
              <table className="file-table">
                <thead>
                  <tr>
                    <th
                      scope="col"
                      aria-sort={sort === "name" ? "ascending" : undefined}
                    >
                      <div className="name-header-content">
                        <span className="file-selection-slot">
                          {!isPublic && selectionMode && (
                            <label className="file-checkbox-target">
                              <input
                                type="checkbox"
                                aria-label="Select All Files on This Page"
                                ref={(input) => {
                                  if (input) {
                                    const count = visibleItems.filter((item) =>
                                      selected.has(item.id),
                                    ).length;
                                    input.indeterminate =
                                      count > 0 && count < visibleItems.length;
                                  }
                                }}
                                checked={
                                  visibleItems.length > 0 &&
                                  visibleItems.every((item) =>
                                    selected.has(item.id),
                                  )
                                }
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
                        </span>
                        <button
                          type="button"
                          className="column-sort-button"
                          aria-label="Sort by Name"
                          onClick={() => {
                            setSort("name");
                            setPage(0);
                          }}
                        >
                          Name
                        </button>
                      </div>
                    </th>
                    <th
                      scope="col"
                      className="date-cell"
                      aria-sort={sort === "modified" ? "descending" : undefined}
                    >
                      <button
                        type="button"
                        className="column-sort-button"
                        aria-label="Sort by Date Added"
                        onClick={() => {
                          setSort("modified");
                          setPage(0);
                        }}
                      >
                        Added
                      </button>
                    </th>
                    <th
                      scope="col"
                      className="size-cell"
                      aria-sort={sort === "size" ? "descending" : undefined}
                    >
                      <button
                        type="button"
                        className="column-sort-button"
                        aria-label="Sort by File Size"
                        onClick={() => {
                          setSort("size");
                          setPage(0);
                        }}
                      >
                        Size
                      </button>
                    </th>
                    <th scope="col" className="actions-cell">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {visibleItems.map((item) => (
                    <tr
                      key={item.id}
                      className={
                        "file-row " + (selected.has(item.id) ? "selected" : "")
                      }
                      data-item-id={item.id}
                      onPointerDown={(event) => {
                        pointerKind.current = event.pointerType;
                        if (
                          (event.target as Element).closest(
                            "a,input,summary,details,button:not(.item-name-button)",
                          )
                        )
                          pendingTouch.current = null;
                      }}
                      onClick={(event) => {
                        if (
                          (event.target as Element).closest(
                            "button,a,input,summary,details",
                          )
                        )
                          return;
                        openPrimary(
                          item,
                          false,
                          event.ctrlKey || event.metaKey,
                          pointerKind.current === "touch",
                        );
                      }}
                      onDoubleClick={(event) => {
                        if (
                          isTrash ||
                          pointerKind.current === "touch" ||
                          (event.target as Element).closest(
                            "a,input,summary,details",
                          )
                        )
                          return;
                        onOpen(item);
                      }}
                    >
                      <td className="name-cell">
                        <div className="name-cell-content">
                          <span className="file-selection-slot">
                            {!isPublic && selectionMode ? (
                              <label className="file-checkbox-target">
                                <input
                                  type="checkbox"
                                  aria-label={"Select " + item.name}
                                  checked={selected.has(item.id)}
                                  onChange={() => toggle(item.id)}
                                />
                              </label>
                            ) : (
                              <FileIcon
                                name={item.name}
                                folder={item.type === "folder"}
                              />
                            )}
                          </span>
                          <div className="file-name-details">
                            <div className="file-title-line">
                              <button
                                className="file-name item-name-button"
                                title={item.name}
                                onClick={(event) =>
                                  openPrimary(
                                    item,
                                    event.detail === 0,
                                    event.ctrlKey || event.metaKey,
                                    pointerKind.current === "touch",
                                  )
                                }
                              >
                                {item.name}
                              </button>
                              {!isPublic && !isTrash && sharing(item) && (
                                <span
                                  className="file-share-indicator"
                                  role="img"
                                  title={sharing(item)}
                                  aria-label={sharing(item)}
                                >
                                  <Link size={14} />
                                </span>
                              )}
                            </div>
                            {isSearch && (
                              <span
                                className="search-result-location"
                                title={[
                                  "My Files",
                                  ...(item.parent_names || []),
                                ].join(" / ")}
                              >
                                {[
                                  "My Files",
                                  ...(item.parent_names || []),
                                ].join(" / ")}
                              </span>
                            )}
                          </div>
                          {item.type === "folder" && (
                            <span className="sr-only">Folder</span>
                          )}
                        </div>
                      </td>
                      <td className="date-cell">
                        {formatDate(item.upload_date)}
                      </td>
                      <td className="size-cell">
                        {item.type === "folder" ? "—" : formatBytes(item.size)}
                      </td>
                      <td className="actions-cell">
                        {isPublic ? (
                          <a
                            className="icon-button"
                            href={
                              item.type === "file"
                                ? fileUrl(itemPath(item), auth, true)
                                : "/?" +
                                  new URLSearchParams({
                                    path: "/share_" + itemPath(item).slice(1),
                                    auth,
                                  })
                            }
                            download={
                              item.type === "file" ? item.name : undefined
                            }
                            aria-label={
                              (item.type === "file" ? "Download " : "Open ") +
                              item.name
                            }
                            onClick={
                              item.type === "folder"
                                ? (event) => {
                                    event.preventDefault();
                                    onOpen(item);
                                  }
                                : undefined
                            }
                          >
                            {item.type === "file" ? (
                              <Download size={18} />
                            ) : (
                              <ChevronRight size={18} />
                            )}
                          </a>
                        ) : (
                          <details
                            className="item-actions"
                            name="file-actions"
                            onToggle={(event) => {
                              if (event.target === event.currentTarget)
                                positionItemMenu(event.currentTarget);
                            }}
                          >
                            <summary
                              className="icon-button"
                              aria-label={"Actions for " + item.name}
                            >
                              <MoreHorizontal size={20} />
                            </summary>
                            <div className="action-menu" popover="manual">
                              {isTrash ? (
                                <>
                                  <button
                                    onClick={() => {
                                      closeItemMenus();
                                      onAction("restore", [item]);
                                    }}
                                  >
                                    <RefreshCw size={16} /> Restore
                                  </button>
                                  <button
                                    className="danger-text"
                                    onClick={() =>
                                      showDialog({
                                        type: "delete",
                                        items: [item],
                                      })
                                    }
                                  >
                                    <Trash2 size={16} /> Delete Permanently
                                  </button>
                                </>
                              ) : (
                                <>
                                  <button
                                    onClick={() => {
                                      closeItemMenus();
                                      onOpen(item);
                                    }}
                                  >
                                    <Eye size={16} /> Open
                                  </button>
                                  {item.type === "file" && (
                                    <>
                                      <a
                                        href={fileUrl(itemPath(item), "", true)}
                                        download={item.name}
                                        onClick={closeItemMenus}
                                      >
                                        <Download size={16} /> Download
                                      </a>
                                      <button
                                        role="menuitem"
                                        onClick={() =>
                                          showDialog({
                                            type: "download-link",
                                            item,
                                          })
                                        }
                                      >
                                        <Link size={16} /> Copy Download Link
                                      </button>
                                      <button
                                        role="menuitem"
                                        onClick={() =>
                                          showDialog({ type: "prepare", item })
                                        }
                                      >
                                        <Download size={16} /> Prepare for
                                        Download / Streaming
                                      </button>
                                    </>
                                  )}
                                  <button
                                    onClick={() =>
                                      showDialog({ type: "rename", item })
                                    }
                                  >
                                    <Pencil size={16} /> Rename
                                  </button>
                                  <button
                                    onClick={() =>
                                      showDialog({
                                        type: "move",
                                        items: [item],
                                      })
                                    }
                                  >
                                    <Move size={16} /> Move
                                  </button>
                                  <button
                                    onClick={() =>
                                      showDialog({ type: "share", item })
                                    }
                                  >
                                    <Link size={16} /> Share Link
                                  </button>
                                  <button
                                    className="danger-text"
                                    onClick={() => {
                                      closeItemMenus();
                                      onAction("trash", [item]);
                                    }}
                                  >
                                    <Trash2 size={16} /> Move to Trash
                                  </button>
                                </>
                              )}
                            </div>
                          </details>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {searchPagination &&
              (searchPagination.offset > 0 || searchPagination.hasMore) && (
                <div className="pagination">
                  <span>
                    Results {searchPagination.offset + 1}–
                    {searchPagination.offset + allItems.length}
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
                  {Math.min((page + 1) * 100, allItems.length)} of{" "}
                  {allItems.length}
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
                  disabled={(page + 1) * 100 >= allItems.length}
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
