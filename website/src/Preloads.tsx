import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Trash2, X, AlertCircle, Settings2 } from "lucide-react";
import { errorMessage } from "./api";
import { ErrorNotice, Spinner, FileIcon, Modal } from "./components";
import { formatByteProgress } from "./types";
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
}: {
  file: CachedFile;
  status?: string;
}) {
  const ranges = coverageRanges(file.ready_ranges || [], file.size);
  const ready = ranges.reduce((sum, [start, end]) => sum + end - start, 0);
  const complete = cachedFileComplete(file);
  const percent =
    file.size > 0 ? Math.min(100, Math.floor((ready / file.size) * 100)) : 0;
  return (
    <div className="cache-coverage" data-complete={complete}>
      <div className="cached-file-meta">
        <span>
          {formatByteProgress(ready, file.size)} cached · {percent}%
        </span>
        <span className="cache-ready-state" data-complete={complete}>
          {complete ? "Ready" : status || "Partial"}
        </span>
      </div>
      <svg
        role="img"
        aria-label={`${formatByteProgress(ready, file.size)} cached · ${percent}% · ${complete ? "Ready" : "Partial"}`}
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
export function cachedFileComplete(file: CachedFile): boolean {
  const ranges = coverageRanges(file.ready_ranges || [], file.size);
  return (
    ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === file.size
  );
}
function jobLabel(job: PreloadJob) {
  return job.state === "ready"
    ? job.target_bytes >= job.size
      ? "Preparation finished"
      : "Ahead region prepared"
    : job.state === "preparing"
      ? "Preparing"
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
  onNotice?: (message: string) => void;
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
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const value = await performanceApi.preloads(signal);
      if (!signal?.aborted) {
        setSnapshot(value);
        setError("");
      }
    } catch (cause) {
      if (!signal?.aborted) setError(errorMessage(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
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
      setNotice(message);
      onNotice?.(message);
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
      (filter === "all" || cachedFileComplete(file) === (filter === "ready")),
  );
  const jobs = (snapshot?.jobs || []).filter((job) =>
    (job.name || "File preparation")
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
  const renderJob = (job: PreloadJob) => (
    <li key={job.id} className="preload-job" data-state={job.state}>
      <div className="preload-job-copy">
        <strong>{job.name || "File preparation"}</strong>
        <span>
          {jobLabel(job)} · {job.mode === "auto" ? "Automatic" : "Manual"}
        </span>
        {["queued", "preparing"].includes(job.state) && (
          <>
            <progress
              max={Math.max(1, job.target_bytes)}
              value={Math.min(job.bytes_done, job.target_bytes)}
              aria-label={"Preparation of " + (job.name || "file")}
            />
            <span>{formatByteProgress(job.bytes_done, job.target_bytes)}</span>
          </>
        )}
        {job.error && (
          <p className="preload-job-error">
            <AlertCircle size={15} />
            {job.error}
          </p>
        )}
      </div>
      {["queued", "preparing"].includes(job.state) && (
        <button
          className="secondary-button"
          disabled={!!busy}
          onClick={() =>
            void act(
              "job:" + job.id,
              () => performanceApi.cancel(job.id),
              "Preparation cancelled. Shared readers can continue.",
            )
          }
          aria-label={"Cancel preparation of " + (job.name || "file")}
        >
          {busy === "job:" + job.id ? <Spinner /> : <X size={16} />} Cancel
        </button>
      )}
    </li>
  );
  return (
    <div className="preloads-page page-body">
      <div className="performance-section-heading">
        <div>
          <p>Reuse downloaded media for faster playback and sharing.</p>
        </div>
        <button
          className="secondary-button"
          disabled={!!busy}
          onClick={() => setRefresh((value) => value + 1)}
          aria-label="Refresh Cache Inventory"
        >
          <RefreshCw size={16} />
          <span>Refresh</span>
        </button>
      </div>
      {loading && (
        <p className="performance-loading" role="status">
          <Spinner /> Loading cache inventory…
        </p>
      )}
      <ErrorNotice message={error} />
      {cache && (
        <>
          <dl className="performance-facts cache-budget-summary">
            <div>
              <dt>Memory cache</dt>
              <dd>
                {formatByteProgress(
                  cache.memory.bytes,
                  cache.memory.effective_max_bytes,
                )}
              </dd>
              <progress
                aria-label="Memory cache usage"
                value={cache.memory.bytes}
                max={Math.max(1, cache.memory.effective_max_bytes)}
              />
            </div>
            <div>
              <dt>Disk cache</dt>
              <dd>
                {cache.disk.available
                  ? formatByteProgress(cache.disk.bytes, cache.disk.max_bytes)
                  : "Unavailable"}
              </dd>
              <progress
                aria-label="Disk cache usage"
                value={cache.disk.bytes}
                max={Math.max(1, cache.disk.max_bytes)}
              />
            </div>
            <div>
              <dt>Preparation</dt>
              <dd>
                {snapshot.active_jobs
                  ? snapshot.active_jobs + " running"
                  : "Idle"}
              </dd>
              <small>
                <a
                  className="inline-link"
                  href="/?view=settings&section=performance"
                >
                  <Settings2 size={14} /> Cache settings
                </a>
              </small>
            </div>
          </dl>
          <section
            className="performance-section"
            aria-labelledby="cached-files-heading"
          >
            <div className="performance-section-heading">
              <div>
                <h3 id="cached-files-heading">
                  Cached files{" "}
                  <span className="performance-count">{files.length}</span>
                </h3>
                <p>Media from playback, downloads and manual preparation.</p>
              </div>
              <button
                className="secondary-button"
                disabled={
                  !!busy ||
                  (!files.length &&
                    !pending.some((job) => job.state !== "failed"))
                }
                onClick={() => setClearing({})}
              >
                <Trash2 size={16} />
                {busy === "clear" ? "Clearing…" : "Clear Cache"}
              </button>
            </div>
            {!!files.length && (
              <div className="cache-inventory-toolbar">
                <div
                  className="performance-filters"
                  role="group"
                  aria-label="Filter cached files"
                >
                  {(
                    [
                      ["all", "All", files.length],
                      ["ready", "Ready", readyCount],
                      ["partial", "Partial", files.length - readyCount],
                    ] as const
                  ).map(([key, label, count]) => (
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
              </div>
            )}
            {files.length ? (
              <ul className="cached-file-list">
                {visibleFiles.map((file) => {
                  const job = pending.find((job) => job.file_id === file.id);
                  return (
                    <li className="cached-file" key={file.id}>
                      <FileIcon name={file.name || "Cached file"} />
                      <div className="cached-file-copy">
                        <div className="cached-file-title">
                          <strong title={file.name || "Cached file"}>
                            {file.name || "Cached file"}
                          </strong>
                        </div>
                        <CacheCoverage
                          file={file}
                          status={
                            job?.state === "preparing"
                              ? "Partial · Loading"
                              : job?.state === "queued"
                                ? "Partial · Queued"
                                : job?.state === "failed"
                                  ? "Needs attention"
                                  : undefined
                          }
                        />
                        {job?.error && (
                          <p className="preload-job-error">
                            <AlertCircle size={15} />
                            {job.error}
                          </p>
                        )}
                      </div>
                      {job && ["queued", "preparing"].includes(job.state) && (
                        <button
                          className="icon-button"
                          disabled={!!busy}
                          aria-label={
                            "Cancel preparation of " + (file.name || "file")
                          }
                          onClick={() =>
                            void act(
                              "job:" + job.id,
                              () => performanceApi.cancel(job.id),
                              "Preparation cancelled. Shared readers can continue.",
                            )
                          }
                        >
                          <X size={17} />
                        </button>
                      )}
                      <button
                        className="icon-button"
                        disabled={!!busy}
                        aria-label={
                          "Evict cached bytes for " + (file.name || "file")
                        }
                        title="Evict cached bytes"
                        onClick={() =>
                          setClearing({
                            id: file.id,
                            name: file.name || "Cached file",
                          })
                        }
                      >
                        {busy === "file:" + file.id ? (
                          <Spinner />
                        ) : (
                          <Trash2 size={17} />
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="performance-empty">
                No media bytes are cached yet. Play a file or start preparation
                from My Files.
              </p>
            )}
            {!!files.length && !visibleFiles.length && (
              <p className="performance-empty">
                No cached files match this filter. Try another search or choose
                All.
              </p>
            )}
            {!!files.length && (
              <p className="settings-help cache-clear-help">
                Clearing cache removes server copies and stops background
                preparation. Your originals stay in Telegram.
              </p>
            )}
          </section>
          {unlistedJobs.length > 0 && (
            <section
              className="performance-section"
              aria-labelledby="preparation-heading"
            >
              <h3 id="preparation-heading">Preparation queue</h3>
              <ul className="preload-jobs">{unlistedJobs.map(renderJob)}</ul>
            </section>
          )}
          {finished.length > 0 && (
            <section
              className="performance-section preload-recent"
              aria-labelledby="recent-preparation-heading"
            >
              <h3 id="recent-preparation-heading">Recently stopped</h3>
              <ul className="preload-jobs">
                {finished.slice(0, 5).map(renderJob)}
              </ul>
            </section>
          )}
          {notice && (
            <p className="performance-notice" role="status">
              {notice}
            </p>
          )}
        </>
      )}
      {clearing && (
        <Modal
          title={clearing.id ? "Clear cached file?" : "Clear all cached files?"}
          onClose={() => setClearing(null)}
          initialFocus=".preload-keep-cache"
        >
          <p className="dialog-description">
            {clearing.id
              ? `Remove the server copy of “${clearing.name}”?`
              : "Remove all cached server copies and stop background preparation?"}{" "}
            Originals stay in Telegram. Future playback or downloads may need to
            fetch the data again.
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
              {clearing.id ? "Clear Cached File" : "Clear All Cache"}
            </button>
          </div>
        </Modal>
      )}
      {!snapshot && !loading && error && (
        <button
          className="secondary-button"
          onClick={() => setRefresh((value) => value + 1)}
        >
          Try Again
        </button>
      )}
    </div>
  );
}
