import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, Trash2, X, AlertCircle, Settings2 } from "lucide-react";
import { errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner, FileIcon, Modal } from "../../shared/components";
import { formatByteProgress, formatBytes } from "../../shared/types";
import { useManualRefresh } from "../../shared/useManualRefresh";
import {
    performanceApi,
    type CachedFile,
    type PreloadJob,
    type PreloadSnapshot,
} from "./performanceApi";
import "./Performance.css";
import "./Preloads.css";

/** Normalize half-open byte ranges before painting coverage; never paint past EOF. */
export function coverageRanges(
    ranges: [number, number][],
    size: number,
): [number, number][] {
    if (!Number.isFinite(size) || size <= 0) return [];
    const clean = ranges
        .filter(
            ([start, end]) =>
                Number.isFinite(start) && Number.isFinite(end) && end > start,
        )
        .map(
            ([start, end]) =>
                [Math.max(0, start), Math.min(size, end)] as [number, number],
        )
        .filter(([start, end]) => end > start)
        .sort((a, b) => a[0] - b[0]);
    const merged: [number, number][] = [];
    for (const [start, end] of clean) {
        const previous = merged.at(-1);
        if (previous && start <= previous[1])
            previous[1] = Math.max(previous[1], end);
        else merged.push([start, end]);
    }
    return merged;
}
export function CacheCoverage({
    file,
    status,
    showName = false,
}: {
    file: CachedFile;
    status?: string;
    showName?: boolean;
}) {
    const ranges = coverageRanges(file.ready_ranges || [], file.size);
    const ready = ranges.reduce((sum, [start, end]) => sum + end - start, 0);
    const complete = cachedFileComplete(file);
    const percent =
        file.size > 0
            ? Math.min(100, Math.floor((ready / file.size) * 100))
            : 0;
    return (
        <div className="cache-coverage" data-complete={complete}>
            <div className="cached-file-heading">
                {showName && (
                    <strong title={file.name || "Cached file"}>
                        {file.name || "Cached file"}
                    </strong>
                )}
                <span className="cache-ready-state" data-complete={complete}>
                    {complete ? "Ready" : status || "Partial"}
                </span>
            </div>
            <div className="cached-file-meta">
                <span>{formatByteProgress(ready, file.size)} cached</span>
                <span>{percent}%</span>
            </div>
            <svg
                role="img"
                aria-label={`${formatByteProgress(ready, file.size)} cached · ${percent}% · ${complete ? "Ready" : status || "Partial"}`}
                viewBox="0 0 1000 8"
                preserveAspectRatio="none"
            >
                <rect
                    x="0"
                    y="0"
                    width="1000"
                    height="8"
                    className="cache-coverage-track"
                />
                {ranges.map(([start, end], index) => (
                    <rect
                        key={index}
                        x={(start / file.size) * 1000}
                        y="0"
                        width={((end - start) / file.size) * 1000}
                        height="8"
                        className="cache-coverage-ready"
                    />
                ))}
            </svg>
        </div>
    );
}
export function CacheCapacity({
    label,
    used,
    limit,
    disabled = false,
    unavailable = false,
}: {
    label: string;
    used: number;
    limit: number;
    disabled?: boolean;
    unavailable?: boolean;
}) {
    const available = !disabled && !unavailable && limit > 0;
    const percent = available ? Math.round((used / limit) * 100) : 0;
    return (
        <div className="preload-capacity-tier" role="group" aria-label={label}>
            <h4>{label}</h4>
            <div className="preload-capacity-value">
                {available ? (
                    <>
                        <strong>{formatBytes(used)}</strong>
                        <span>of {formatBytes(limit)} used</span>
                        <span>{percent}%</span>
                    </>
                ) : (
                    <strong>{disabled ? "Disabled" : "Unavailable"}</strong>
                )}
            </div>
            {available ? (
                <progress
                    aria-label={label + " usage"}
                    aria-valuetext={`${formatByteProgress(used, limit)} used (${percent}%)`}
                    max={limit}
                    value={Math.min(used, limit)}
                />
            ) : (
                <div
                    className="preload-capacity-unavailable"
                    aria-hidden="true"
                />
            )}
        </div>
    );
}
export function cachedFileComplete(file: CachedFile): boolean {
    const ranges = coverageRanges(file.ready_ranges || [], file.size);
    return (
        ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === file.size
    );
}
/** Snapshots are newest first; older outcomes do not describe a file's current job. */
export function currentPreloadJobs(jobs: PreloadJob[]): PreloadJob[] {
    const seen = new Set<string>();
    return jobs.filter((job) => {
        if (seen.has(job.file_id)) return false;
        seen.add(job.file_id);
        return true;
    });
}
function jobLabel(job: PreloadJob) {
    return job.state === "ready"
        ? job.target_bytes >= job.size
            ? "Preload finished"
            : "Upcoming chunks cached"
        : job.state === "preparing"
          ? "Preloading"
          : job.state === "failed"
            ? "Needs attention"
            : job.state === "cancelled"
              ? "Cancelled"
              : "Queued";
}
export default function Preloads({
    onError,
    onNotice,
    query = "",
}: {
    onError?: (message: string) => void;
    onNotice?: (message: string, error?: boolean) => void;
    query?: string;
} = {}) {
    const [snapshot, setSnapshot] = useState<PreloadSnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [busy, setBusy] = useState("");
    const [clearing, setClearing] = useState<{
        id?: string;
        name?: string;
    } | null>(null);
    const [refresh, setRefresh] = useState(0);
    const [filter, setFilter] = useState<"all" | "ready" | "partial">("all");
    const latestLoad = useRef(0);
    const manualLoading = useRef(false);
    const { refreshing, run } = useManualRefresh(
        onNotice ||
            ((message, failed) => {
                if (failed) setError(message);
                else setNotice(message);
            }),
    );
    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        if (!manual && manualLoading.current) return false;
        const request = ++latestLoad.current;
        try {
            const value = await performanceApi.preloads(signal);
            if (!signal?.aborted && request === latestLoad.current) {
                setSnapshot(value);
                setError("");
                return true;
            }
            return false;
        } catch (cause) {
            if (!signal?.aborted && request === latestLoad.current)
                setError(errorMessage(cause));
            if (manual) throw cause;
            return false;
        } finally {
            if (!signal?.aborted && request === latestLoad.current)
                setLoading(false);
        }
    }, []);
    const refreshInventory = () =>
        void run(async (signal) => {
            manualLoading.current = true;
            setNotice("");
            try {
                return await load(signal, true);
            } finally {
                manualLoading.current = false;
            }
        }, "Cache inventory refreshed.");
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            await load(controller.signal);
            if (!controller.signal.aborted)
                timer = setTimeout(poll, document.hidden ? 15000 : 3000);
        };
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [load, refresh]);
    const act = async (
        key: string,
        task: () => Promise<unknown>,
        message: string,
    ) => {
        setBusy(key);
        setError("");
        setNotice("");
        try {
            await task();
            if (onNotice) onNotice(message);
            else setNotice(message);
            setRefresh((value) => value + 1);
        } catch (cause) {
            const message = errorMessage(cause);
            setError(message);
            onError?.(message);
        } finally {
            setBusy("");
        }
    };
    const cache = snapshot?.cache;
    const files = cache?.files || [];
    const readyCount = files.filter(cachedFileComplete).length;
    const visibleFiles = files.filter(
        (file) =>
            (file.name || "Cached file")
                .toLocaleLowerCase()
                .includes(query.trim().toLocaleLowerCase()) &&
            (filter === "all" ||
                cachedFileComplete(file) === (filter === "ready")),
    );
    const currentJobs = currentPreloadJobs(snapshot?.jobs || []);
    const jobs = currentJobs.filter((job) =>
        (job.name || "File preload")
            .toLocaleLowerCase()
            .includes(query.trim().toLocaleLowerCase()),
    );
    const pending = jobs.filter((job) =>
        ["queued", "preparing", "failed"].includes(job.state),
    );
    const unlistedJobs = pending.filter(
        (job) => !files.some((file) => file.id === job.file_id),
    );
    const finished = jobs.filter(
        (job) =>
            job.state === "cancelled" &&
            !files.some((file) => file.id === job.file_id),
    );
    const hasActiveJobs = currentJobs.some((job) =>
        ["queued", "preparing"].includes(job.state),
    );
    const filters = [
        ["all", "All", files.length],
        ["ready", "Ready", readyCount],
        ["partial", "Partial", files.length - readyCount],
    ] as const;
    const renderJob = (job: PreloadJob) => (
        <li key={job.id} className="preload-job" data-state={job.state}>
            <div className="preload-job-copy">
                <strong>{job.name || "File preload"}</strong>
                <span>
                    {jobLabel(job)} ·{" "}
                    {job.mode === "auto" ? "Automatic" : "Manual"}
                </span>
                {["queued", "preparing"].includes(job.state) && (
                    <div className="preload-job-progress">
                        <progress
                            max={Math.max(1, job.target_bytes)}
                            value={Math.min(job.bytes_done, job.target_bytes)}
                            aria-label={"Preloading " + (job.name || "file")}
                        />
                        <span>
                            {formatByteProgress(
                                job.bytes_done,
                                job.target_bytes,
                            )}
                        </span>
                    </div>
                )}
                {job.error && (
                    <p className="preload-job-error">
                        <AlertCircle size={15} aria-hidden="true" />
                        {job.error}
                    </p>
                )}
            </div>
            {["queued", "preparing"].includes(job.state) && (
                <button
                    className="secondary-button"
                    disabled={!!busy || refreshing}
                    onClick={() =>
                        void act(
                            "job:" + job.id,
                            () => performanceApi.cancel(job.id),
                            "Preload cancelled. Active streams and downloads can continue.",
                        )
                    }
                    aria-label={"Cancel preload of " + (job.name || "file")}
                >
                    {busy === "job:" + job.id ? (
                        <Spinner />
                    ) : (
                        <X size={16} aria-hidden="true" />
                    )}{" "}
                    Cancel
                </button>
            )}
        </li>
    );
    return (
        <div className="preloads-page page-body">
            {loading && (
                <p className="performance-loading" role="status">
                    <Spinner /> Loading cache inventory…
                </p>
            )}
            <ErrorNotice message={error} />
            {cache && (
                <>
                    <section
                        className="preload-capacity"
                        aria-labelledby="preload-capacity-heading"
                    >
                        <div className="preload-capacity-header">
                            <h3 id="preload-capacity-heading">
                                Cache capacity
                            </h3>
                            <span className="preload-capacity-activity">
                                {snapshot.active_jobs
                                    ? snapshot.active_jobs +
                                      (snapshot.active_jobs === 1
                                          ? " preload running"
                                          : " preloads running")
                                    : "No preloads running"}
                            </span>
                            <a
                                className="inline-link"
                                href="/?view=settings&section=performance"
                            >
                                <Settings2 size={15} aria-hidden="true" /> Cache
                                settings
                            </a>
                        </div>
                        <div
                            className="preload-capacity-meters"
                            role="group"
                            aria-label="Server cache usage"
                        >
                            <CacheCapacity
                                label="Memory cache"
                                used={cache.memory.bytes}
                                limit={cache.memory.effective_max_bytes}
                                disabled={cache.memory.max_bytes === 0}
                                unavailable={
                                    cache.memory.effective_max_bytes <= 0
                                }
                            />
                            <CacheCapacity
                                label="Disk cache"
                                used={cache.disk.bytes}
                                limit={cache.disk.max_bytes}
                                disabled={cache.disk.max_bytes === 0}
                                unavailable={!cache.disk.available}
                            />
                        </div>
                    </section>
                    <section
                        className="preload-inventory"
                        aria-labelledby="cached-files-heading"
                    >
                        <h3 id="cached-files-heading">Cached files</h3>
                        <div className="preload-inventory-toolbar">
                            <div
                                className="preload-filters"
                                role="group"
                                aria-label="Filter cached files"
                            >
                                {filters.map(([key, label, count]) => (
                                    <button
                                        type="button"
                                        key={key}
                                        aria-pressed={filter === key}
                                        onClick={() => setFilter(key)}
                                    >
                                        {label} <span>{count}</span>
                                    </button>
                                ))}
                            </div>
                            <select
                                className="preload-filter-select"
                                aria-label="Filter cached files"
                                value={filter}
                                onChange={(event) =>
                                    setFilter(
                                        event.currentTarget
                                            .value as typeof filter,
                                    )
                                }
                            >
                                {filters.map(([key, label, count]) => (
                                    <option key={key} value={key}>
                                        {label === "All" ? "All files" : label}{" "}
                                        ({count})
                                    </option>
                                ))}
                            </select>
                            <div className="preload-inventory-actions">
                                <button
                                    className="secondary-button"
                                    disabled={!!busy || refreshing}
                                    aria-busy={refreshing}
                                    onClick={refreshInventory}
                                    aria-label="Refresh Cache Inventory"
                                    title="Refresh cache inventory"
                                >
                                    {refreshing ? (
                                        <Spinner />
                                    ) : (
                                        <RefreshCw
                                            size={16}
                                            aria-hidden="true"
                                        />
                                    )}
                                    <span>
                                        {refreshing ? "Refreshing…" : "Refresh"}
                                    </span>
                                </button>
                                <button
                                    className="secondary-button"
                                    disabled={
                                        !!busy ||
                                        refreshing ||
                                        (!files.length && !hasActiveJobs)
                                    }
                                    onClick={() => setClearing({})}
                                    aria-label="Clear Cache"
                                    title="Clear all cached files"
                                >
                                    {busy === "clear" ? (
                                        <Spinner />
                                    ) : (
                                        <Trash2 size={16} aria-hidden="true" />
                                    )}
                                    <span>
                                        {busy === "clear"
                                            ? "Clearing…"
                                            : "Clear cache"}
                                    </span>
                                </button>
                            </div>
                        </div>
                        {files.length ? (
                            <>
                                <ul
                                    className="cached-file-list"
                                    aria-label="Cached files"
                                >
                                    {visibleFiles.map((file) => {
                                        const job = pending.find(
                                            (job) => job.file_id === file.id,
                                        );
                                        return (
                                            <li
                                                className="cached-file"
                                                key={file.id}
                                            >
                                                <FileIcon
                                                    name={
                                                        file.name ||
                                                        "Cached file"
                                                    }
                                                />
                                                <CacheCoverage
                                                    file={file}
                                                    showName
                                                    status={
                                                        job?.state ===
                                                        "preparing"
                                                            ? "Partial · Preloading"
                                                            : job?.state ===
                                                                "queued"
                                                              ? "Partial · Queued"
                                                              : job?.state ===
                                                                  "failed"
                                                                ? "Needs attention"
                                                                : undefined
                                                    }
                                                />
                                                <div className="cached-file-actions">
                                                    {job &&
                                                        [
                                                            "queued",
                                                            "preparing",
                                                        ].includes(
                                                            job.state,
                                                        ) && (
                                                            <button
                                                                className="icon-button"
                                                                disabled={
                                                                    !!busy ||
                                                                    refreshing
                                                                }
                                                                aria-label={
                                                                    "Cancel preload of " +
                                                                    (file.name ||
                                                                        "file")
                                                                }
                                                                title="Cancel preload"
                                                                onClick={() =>
                                                                    void act(
                                                                        "job:" +
                                                                            job.id,
                                                                        () =>
                                                                            performanceApi.cancel(
                                                                                job.id,
                                                                            ),
                                                                        "Preload cancelled. Active streams and downloads can continue.",
                                                                    )
                                                                }
                                                            >
                                                                {busy ===
                                                                "job:" +
                                                                    job.id ? (
                                                                    <Spinner />
                                                                ) : (
                                                                    <X
                                                                        size={
                                                                            17
                                                                        }
                                                                        aria-hidden="true"
                                                                    />
                                                                )}
                                                            </button>
                                                        )}
                                                    <button
                                                        className="icon-button"
                                                        disabled={
                                                            !!busy || refreshing
                                                        }
                                                        aria-label={
                                                            "Evict cached bytes for " +
                                                            (file.name ||
                                                                "file")
                                                        }
                                                        title="Clear cached bytes"
                                                        onClick={() =>
                                                            setClearing({
                                                                id: file.id,
                                                                name:
                                                                    file.name ||
                                                                    "Cached file",
                                                            })
                                                        }
                                                    >
                                                        {busy ===
                                                        "file:" + file.id ? (
                                                            <Spinner />
                                                        ) : (
                                                            <Trash2
                                                                size={17}
                                                                aria-hidden="true"
                                                            />
                                                        )}
                                                    </button>
                                                </div>
                                                {job?.error && (
                                                    <p className="preload-job-error cached-file-error">
                                                        <AlertCircle
                                                            size={15}
                                                            aria-hidden="true"
                                                        />
                                                        {job.error}
                                                    </p>
                                                )}
                                            </li>
                                        );
                                    })}
                                </ul>
                            </>
                        ) : (
                            <div className="preload-empty">
                                <p>No files cached yet</p>
                                <span>
                                    In My Files, choose{" "}
                                    <strong>Preload file</strong> from a file’s
                                    menu.
                                </span>
                            </div>
                        )}
                        {!!files.length && !visibleFiles.length && (
                            <p className="performance-empty">
                                No cached files match this filter. Try another
                                search or choose All.
                            </p>
                        )}
                    </section>
                    {unlistedJobs.length > 0 && (
                        <section
                            className="preload-queue"
                            aria-labelledby="preparation-heading"
                        >
                            <h3 id="preparation-heading">Preload queue</h3>
                            <ul className="preload-jobs">
                                {unlistedJobs.map(renderJob)}
                            </ul>
                        </section>
                    )}
                    {finished.length > 0 && (
                        <details className="preload-recent">
                            <summary>
                                Recently stopped ({finished.length})
                            </summary>
                            <ul className="preload-jobs">
                                {finished.slice(0, 5).map(renderJob)}
                            </ul>
                        </details>
                    )}
                    {notice && (
                        <p className="performance-notice" role="status">
                            {notice}
                        </p>
                    )}
                </>
            )}
            <details className="preload-help">
                <summary>About preloading</summary>
                <div>
                    <p>
                        Choose <strong>Preload file</strong> from a file’s menu
                        in My Files to cache the whole file on the server.
                        Follow its progress here; <strong>Ready</strong> means
                        every byte is currently cached.
                    </p>
                    <p>
                        Media playback fetches requested chunks with bounded
                        read-ahead. Automatic preloading can warm additional
                        upcoming playback chunks; downloads fetch their
                        requested ranges.
                    </p>
                    <p>
                        Cache space is shared. Cached bytes may be removed when
                        space is needed. Clearing cache removes server copies
                        and stops preloading; originals stay in Telegram. Your
                        device still needs a connection to the server.
                    </p>
                    <a
                        className="inline-link"
                        href="https://github.com/TechShreyash/TGDrive/blob/main/docs/usage.md#preload-a-file-for-smoother-playback-and-downloads"
                        target="_blank"
                        rel="noopener noreferrer"
                    >
                        Read the preloading guide
                    </a>
                </div>
            </details>
            {clearing && (
                <Modal
                    title={
                        clearing.id
                            ? "Clear cached file?"
                            : "Clear all cached files?"
                    }
                    onClose={() => setClearing(null)}
                    initialFocus=".preload-keep-cache"
                >
                    <p className="dialog-description">
                        {clearing.id
                            ? `Remove the server copy of “${clearing.name}”?`
                            : "Remove all cached server copies and stop background preloading?"}{" "}
                        Originals stay in Telegram. Future playback or downloads
                        may need to fetch the data again.
                    </p>
                    <div className="dialog-actions">
                        <button
                            type="button"
                            className="secondary-button preload-keep-cache"
                            onClick={() => setClearing(null)}
                        >
                            Keep Cache
                        </button>
                        <button
                            type="button"
                            className="danger-button"
                            onClick={() => {
                                const target = clearing;
                                setClearing(null);
                                void act(
                                    target.id ? "file:" + target.id : "clear",
                                    () => performanceApi.clear(target.id),
                                    target.id
                                        ? "Cached bytes evicted. The original is unchanged."
                                        : "Cached bytes cleared. Originals remain in Telegram.",
                                );
                            }}
                        >
                            {clearing.id
                                ? "Clear Cached File"
                                : "Clear All Cache"}
                        </button>
                    </div>
                </Modal>
            )}
            {!snapshot && !loading && error && (
                <button
                    className="secondary-button"
                    disabled={refreshing}
                    aria-busy={refreshing}
                    onClick={refreshInventory}
                >
                    {refreshing && <Spinner />}
                    {refreshing ? "Refreshing…" : "Try Again"}
                </button>
            )}
        </div>
    );
}
