import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import { formatBytes } from "./types";
import StreamMemoryFact from "./StreamMemoryFact";
import {
  performanceApi,
  type LoadSample,
  type PerformanceSnapshot,
  type TrafficSample,
} from "./performanceApi";
import "./Performance.css";

export function queueWait(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds))
    return "Estimating…";
  if (seconds < 1) return "Less than 1s";
  if (seconds < 60) return "About " + Math.ceil(seconds) + "s";
  return "About " + Math.ceil(seconds / 60) + "m";
}
export function httpRates(history: TrafficSample[]): LoadSample[] {
  const rates: LoadSample[] = [];
  for (let index = 1; index < history.length; index++) {
    const previous = history[index - 1],
      current = history[index];
    const seconds = current.at - previous.at;
    if (
      seconds <= 0 ||
      !Number.isFinite(seconds) ||
      current.download_bytes < previous.download_bytes ||
      current.upload_bytes < previous.upload_bytes
    )
      continue;
    rates.push({
      at: current.at,
      active: (current.download_bytes - previous.download_bytes) / seconds,
      queued: (current.upload_bytes - previous.upload_bytes) / seconds,
    });
  }
  return rates;
}
export function loadPoints(
  samples: LoadSample[],
  key: "active" | "queued",
  max: number,
): string {
  const valid = samples.filter(
    (sample) => Number.isFinite(sample[key]) && Number.isFinite(sample.at),
  );
  if (!valid.length) return "";
  const first = valid[0].at;
  const duration = valid[valid.length - 1].at - first;
  return valid
    .map(
      (sample) =>
        (duration <= 0 ? 0 : ((sample.at - first) / duration) * 600) +
        "," +
        (120 - (Math.max(0, sample[key]) / Math.max(1, max)) * 110),
    )
    .join(" ");
}
export default function PerformancePanel() {
  const [snapshot, setSnapshot] = useState<PerformanceSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await performanceApi.snapshot(controller.signal);
        if (!controller.signal.aborted) {
          setSnapshot(value);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = setTimeout(poll, document.hidden ? 15000 : 3000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh]);
  const telegram = snapshot?.telegram;
  const history = telegram?.history || [];
  const ceiling = Math.max(
    1,
    ...history.flatMap((sample) => [sample.active, sample.queued]),
  );
  const traffic = snapshot?.traffic;
  const rates = httpRates(traffic?.history || []);
  const rateCeiling = Math.max(
    1,
    ...rates.flatMap((sample) => [sample.active, sample.queued]),
  );
  return (
    <div className="performance-page page-body">
      <div className="performance-section-heading">
        <div>
          <h2>Live activity</h2>
          <p>
            Current server load and Telegram admission. Queue times are
            estimates.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={() => setRefresh((value) => value + 1)}
          aria-label="Refresh Performance"
        >
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>
      {loading && (
        <p className="performance-loading" role="status">
          <Spinner /> Loading server performance…
        </p>
      )}
      <ErrorNotice message={error} />
      {snapshot && telegram && (
        <>
          <dl className="performance-facts">
            <div>
              <dt>Telegram work</dt>
              <dd>
                {telegram.active} active · {telegram.queued} queued
              </dd>
              <small>Queue ceiling {telegram.max_queue}</small>
            </div>
            <div>
              <dt>Server disk free</dt>
              <dd>{formatBytes(snapshot.server.disk.free_bytes)}</dd>
              <small>
                Upload staging budget{" "}
                {formatBytes(snapshot.server.staging_budget_bytes)}
              </small>
            </div>
            <div>
              <dt>Catalog files</dt>
              <dd>
                {snapshot.drive.files.toLocaleString()} files ·{" "}
                {formatBytes(snapshot.drive.total_bytes)}
              </dd>
              <small>{snapshot.drive.folders.toLocaleString()} folders</small>
            </div>
          </dl>
          {traffic && (
            <section
              className="performance-section"
              aria-labelledby="http-traffic-heading"
            >
              <h3 id="http-traffic-heading">HTTP transfer traffic</h3>
              <dl className="performance-facts">
                <div>
                  <dt>Sent by server</dt>
                  <dd>{formatBytes(traffic.download_bytes)}</dd>
                  <small>File/download and receipt response bodies</small>
                </div>
                <div>
                  <dt>Received by server</dt>
                  <dd>{formatBytes(traffic.upload_bytes)}</dd>
                  <small>Upload/receipt request bodies</small>
                </div>
                <div>
                  <dt>Tracked requests</dt>
                  <dd>
                    {traffic.requests.toLocaleString()} total · {traffic.active}{" "}
                    active
                  </dd>
                  <small>
                    {traffic.errors.toLocaleString()} errors; process-local
                    counters
                  </small>
                </div>
              </dl>
              {rates.length >= 2 ? (
                <>
                  <div className="performance-plot-legend">
                    <span>Sent per second</span>
                    <span>Received per second</span>
                  </div>
                  <p className="settings-help">
                    Scale 0–{formatBytes(rateCeiling)}/s · measured differences
                    between recent samples
                  </p>
                  <svg
                    className="performance-load-plot"
                    role="img"
                    aria-label="Measured HTTP send and receive throughput"
                    viewBox="0 0 600 130"
                    preserveAspectRatio="none"
                  >
                    <path d="M0 120H600" className="performance-plot-axis" />
                    <polyline
                      points={loadPoints(rates, "active", rateCeiling)}
                      className="performance-plot-active"
                    />
                    <polyline
                      points={loadPoints(rates, "queued", rateCeiling)}
                      className="performance-plot-queued"
                    />
                  </svg>
                </>
              ) : (
                <p className="performance-empty">
                  Collecting enough samples to show transfer throughput.
                </p>
              )}
              <p className="settings-help">
                Tracks /file and resumable-upload HTTP bodies, including cached
                responses. Excludes settings polls and assets. These counters
                reset on restart and are not physical network-interface
                utilization.
              </p>
            </section>
          )}
          <section
            className="performance-section"
            aria-labelledby="recent-load-heading"
          >
            <h3 id="recent-load-heading">Recent Telegram load</h3>
            {history.length >= 2 ? (
              <>
                <p className="settings-help">
                  0–{ceiling} simultaneous operations ·{" "}
                  {Math.max(
                    0,
                    Math.round(history[history.length - 1].at - history[0].at),
                  )}
                  s of sampled activity
                </p>
                <div className="performance-plot-legend">
                  <span>Active operations</span>
                  <span>Queued operations</span>
                </div>
                <svg
                  className="performance-load-plot"
                  role="img"
                  aria-label={
                    "Recent Telegram load, peak " +
                    ceiling +
                    " concurrent operations"
                  }
                  viewBox="0 0 600 130"
                  preserveAspectRatio="none"
                >
                  <path d="M0 120H600" className="performance-plot-axis" />
                  <polyline
                    points={loadPoints(history, "active", ceiling)}
                    className="performance-plot-active"
                  />
                  <polyline
                    points={loadPoints(history, "queued", ceiling)}
                    className="performance-plot-queued"
                  />
                </svg>
                <p className="settings-help">
                  Recent observations from this running process.{" "}
                  {snapshot.history_scope ||
                    "Restart resets this operational history."}
                </p>
              </>
            ) : (
              <p className="performance-empty">
                Collecting recent observations. Current activity is available
                above.
              </p>
            )}
          </section>
          <section
            className="performance-section"
            aria-labelledby="account-load-heading"
          >
            <h3 id="account-load-heading">Account capacity</h3>
            {telegram.accounts.length ? (
              <div
                className="performance-table-wrap"
                tabIndex={0}
                role="region"
                aria-label="Account capacity; scroll horizontally for more columns"
              >
                <table className="performance-table">
                  <thead>
                    <tr>
                      <th>Account</th>
                      <th>Media operations</th>
                      <th>Origin bytes</th>
                      <th>Part requests</th>
                      <th>Pacing</th>
                      <th>Provider waits</th>
                    </tr>
                  </thead>
                  <tbody>
                    {telegram.accounts.map((account) => (
                      <tr key={account.name}>
                        <th scope="row">
                          {account.name}
                          <small>
                            {account.kind === "user"
                              ? "Premium account"
                              : "Storage bot"}
                          </small>
                        </th>
                        <td>
                          {account.uploads} upload · {account.downloads}{" "}
                          download
                          <small>Combined limit {account.combined_limit}</small>
                        </td>
                        <td>
                          {typeof account.requests?.upload_bytes ===
                          "number" ? (
                            <>
                              {formatBytes(account.requests.upload_bytes)}{" "}
                              uploaded
                              <small>
                                {formatBytes(account.requests.download_bytes)}{" "}
                                downloaded
                              </small>
                            </>
                          ) : (
                            "Unavailable"
                          )}
                        </td>
                        <td>
                          {account.requests
                            ? account.requests.active +
                              " active / " +
                              account.requests.effective_limit +
                              " limit"
                            : "Unavailable"}
                          <small>
                            {account.requests
                              ? account.requests.queued + " requests waiting"
                              : "No request sample"}
                          </small>
                        </td>
                        <td>
                          {account.requests
                            ? account.requests.effective_parts_per_second.toFixed(
                                1,
                              ) + " / second"
                            : "—"}
                          <small>
                            {account.requests
                              ? "Configured ceiling " +
                                account.requests.configured_parts_per_second
                              : ""}
                          </small>
                        </td>
                        <td>
                          {Math.max(
                            account.cooldown_seconds,
                            account.requests?.cooldown_seconds || 0,
                          ) > 0
                            ? queueWait(
                                Math.max(
                                  account.cooldown_seconds,
                                  account.requests?.cooldown_seconds || 0,
                                ),
                              )
                            : "Ready"}
                          <small>
                            {account.requests
                              ? account.requests.floods + " waits observed"
                              : ""}
                          </small>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="performance-empty">
                No account samples are available. Check Connections.
              </p>
            )}
          </section>
          <section
            className="performance-section"
            aria-labelledby="media-queue-heading"
          >
            <h3 id="media-queue-heading">Waiting for Telegram</h3>
            <p className="settings-help">
              Demand reads have priority over background preparation. Estimates
              change with workload and provider cooldowns.
            </p>
            {telegram.queue.length ? (
              <div
                className="performance-table-wrap"
                tabIndex={0}
                role="region"
                aria-label="Telegram queue; scroll horizontally for more columns"
              >
                <table className="performance-table">
                  <thead>
                    <tr>
                      <th>Position</th>
                      <th>Work</th>
                      <th>Priority</th>
                      <th>Waiting</th>
                      <th>Estimated wait</th>
                    </tr>
                  </thead>
                  <tbody>
                    {telegram.queue.map((entry, index) => (
                      <tr key={(entry.request_id || "queue") + index}>
                        <td>{entry.position}</td>
                        <td>
                          {entry.operation === "download"
                            ? "Download"
                            : "Upload"}
                        </td>
                        <td>
                          {entry.priority === "preload"
                            ? "Background"
                            : "Demand"}
                        </td>
                        <td>{Math.floor(entry.waiting_seconds)}s</td>
                        <td>{queueWait(entry.estimated_wait_seconds)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="performance-empty">
                No origin operations are waiting.
              </p>
            )}
          </section>
          <section
            className="performance-section"
            aria-labelledby="server-resource-heading"
          >
            <h3 id="server-resource-heading">Server resources</h3>
            <dl className="performance-facts">
              <div>
                <dt>Server memory available</dt>
                <dd>
                  {typeof snapshot.server.memory.MemAvailable === "number"
                    ? formatBytes(snapshot.server.memory.MemAvailable)
                    : "Unavailable"}
                </dd>
                <small>
                  {typeof snapshot.server.memory.MemTotal === "number"
                    ? "Host total " +
                      formatBytes(snapshot.server.memory.MemTotal)
                    : "Host memory sample not available"}
                </small>
              </div>
              <div>
                <dt>Container memory</dt>
                <dd>
                  {typeof snapshot.server.memory.cgroup_current === "number"
                    ? formatBytes(snapshot.server.memory.cgroup_current)
                    : "Unavailable"}
                </dd>
                <small>
                  {typeof snapshot.server.memory.cgroup_limit === "number"
                    ? "Limit " +
                      formatBytes(snapshot.server.memory.cgroup_limit)
                    : "No finite container limit reported"}
                </small>
              </div>
              <div>
                <dt>Server load average</dt>
                <dd>
                  {snapshot.server.load_average
                    .map((value) => value.toFixed(2))
                    .join(" / ")}
                </dd>
                <small>1 / 5 / 15 minutes; relative to CPU capacity</small>
              </div>
              <div>
                <dt>Process peak memory</dt>
                <dd>{formatBytes(snapshot.server.process_peak_rss_bytes)}</dd>
                <small>Peak resident memory, not current usage</small>
              </div>
              <div>
                <dt>RAM cache</dt>
                <dd>{formatBytes(snapshot.cache.memory.bytes)}</dd>
                <small>
                  Effective limit{" "}
                  {formatBytes(snapshot.cache.memory.effective_max_bytes)}
                </small>
              </div>
              <div>
                <dt>Cache reuse</dt>
                <dd>
                  {snapshot.cache.memory.hits.toLocaleString()} hits ·{" "}
                  {snapshot.cache.memory.misses.toLocaleString()} misses
                </dd>
                <small>Shared cache observations</small>
              </div>
              <StreamMemoryFact scratch={snapshot.cache.scratch} />
            </dl>
          </section>
        </>
      )}
    </div>
  );
}
