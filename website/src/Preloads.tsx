import { useCallback, useEffect, useState } from "react";
import { Database, RefreshCw, Trash2, X, AlertCircle } from "lucide-react";
import { errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import { formatBytes, formatByteProgress } from "./types";
import {
  performanceApi,
  type CachedFile,
  type PreloadJob,
  type PreloadSnapshot,
} from "./performanceApi";
import "./Performance.css";

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
export function CacheCoverage({ file }: { file: CachedFile }) {
  const ranges = coverageRanges(file.ready_ranges || [], file.size);
  const ready = ranges.reduce((sum, [start, end]) => sum + end - start, 0);
  return (
    <div className="cache-coverage">
      <svg
        role="img"
        aria-label={formatByteProgress(ready, file.size) + " cached"}
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
      <span>
        {formatByteProgress(ready, file.size)} cached
        {ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === file.size
          ? " · Complete"
          : ""}
      </span>
    </div>
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
}: {
  onError?: (message: string) => void;
  onNotice?: (message: string) => void;
} = {}) {
  const [snapshot, setSnapshot] = useState<PreloadSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [refresh, setRefresh] = useState(0);
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
  const jobs = snapshot?.jobs || [];
  const pending = jobs.filter((job) =>
    ["queued", "preparing", "failed"].includes(job.state),
  );
  const finished = jobs.filter(
    (job) => !["queued", "preparing", "failed"].includes(job.state),
  );
  const renderJob = (job: PreloadJob) => (
    <li key={job.id} className="preload-job">
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
          <h2>Cache overview</h2>
          <p>
            Prepare a file before sharing, or reuse bytes already fetched during
            playback.
          </p>
        </div>
        <button
          className="secondary-button"
          disabled={!!busy}
          onClick={() => setRefresh((value) => value + 1)}
          aria-label="Refresh Cache Inventory"
        >
          <RefreshCw size={16} />
          Refresh
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
          <dl className="performance-facts">
            <div>
              <dt>Memory cache</dt>
              <dd>
                {formatByteProgress(
                  cache.memory.bytes,
                  cache.memory.effective_max_bytes,
                )}
              </dd>
              <small>
                Effective limit; configured{" "}
                {formatBytes(cache.memory.max_bytes)}
              </small>
            </div>
            <div>
              <dt>Disk cache</dt>
              <dd>
                {cache.disk.available
                  ? formatByteProgress(cache.disk.bytes, cache.disk.max_bytes)
                  : "Unavailable"}
              </dd>
              <small>
                {cache.disk.available
                  ? "Disposable bytes, shared by all viewers"
                  : "Playback falls back to Telegram where possible"}
              </small>
            </div>
            <div>
              <dt>Background preparation</dt>
              <dd>
                {snapshot.active_jobs} / {snapshot.max_background_jobs} active
              </dd>
              <small>Active viewers have priority</small>
            </div>
          </dl>
          <p className="settings-help">
            Cached content can expire or be evicted. Preparation does not pin a
            file permanently; current coverage below is authoritative.
          </p>
          <section
            className="performance-section"
            aria-labelledby="preparation-heading"
          >
            <h3 id="preparation-heading">Preparation jobs</h3>
            {pending.length ? (
              <ul className="preload-jobs">{pending.map(renderJob)}</ul>
            ) : (
              <p className="performance-empty">
                No preparation is running. Choose Prepare for Download /
                Streaming from a file’s menu in My Files to warm it before use.
              </p>
            )}
            {finished.length > 0 && (
              <details className="performance-advanced">
                <summary>Recent preparations ({finished.length})</summary>
                <ul className="preload-jobs">{finished.map(renderJob)}</ul>
              </details>
            )}
          </section>
          <section
            className="performance-section"
            aria-labelledby="cached-files-heading"
          >
            <div className="performance-section-heading">
              <div>
                <h3 id="cached-files-heading">Cached files</h3>
                <p>
                  Includes viewing, downloads and preparation. Clear Cache also
                  stops background preparation.
                </p>
              </div>
              <button
                className="secondary-button"
                disabled={
                  !!busy ||
                  (!files.length &&
                    !pending.some((job) => job.state !== "failed"))
                }
                onClick={() =>
                  void act(
                    "clear",
                    () => performanceApi.clear(),
                    "Cached bytes cleared. Originals remain in Telegram.",
                  )
                }
              >
                <Trash2 size={16} />
                {busy === "clear" ? "Clearing…" : "Clear Cache"}
              </button>
            </div>
            {files.length ? (
              <ul className="cached-file-list">
                {files.map((file) => (
                  <li className="cached-file" key={file.id}>
                    <Database size={20} aria-hidden="true" />
                    <div className="cached-file-copy">
                      <strong>{file.name || "Cached file"}</strong>
                      <CacheCoverage file={file} />
                      <details>
                        <summary>
                          Cached ranges (
                          {coverageRanges(file.ready_ranges, file.size).length})
                        </summary>
                        <p className="settings-help">
                          Byte intervals; the end position is exclusive.
                        </p>
                        <ul className="cached-ranges">
                          {coverageRanges(file.ready_ranges, file.size).map(
                            ([start, end], index) => (
                              <li key={index}>
                                {formatBytes(start)} – {formatBytes(end)}
                              </li>
                            ),
                          )}
                        </ul>
                      </details>
                    </div>
                    <button
                      className="icon-button"
                      disabled={!!busy}
                      aria-label={
                        "Evict cached bytes for " + (file.name || "file")
                      }
                      title="Evict cached bytes"
                      onClick={() =>
                        void act(
                          "file:" + file.id,
                          () => performanceApi.clear(file.id),
                          "Cached bytes evicted. The original is unchanged.",
                        )
                      }
                    >
                      {busy === "file:" + file.id ? (
                        <Spinner />
                      ) : (
                        <Trash2 size={17} />
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="performance-empty">
                No media bytes are cached yet. Play a file or start preparation
                from My Files.
              </p>
            )}
          </section>
          <p className="performance-notice" role="status">
            {notice}
          </p>
        </>
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
