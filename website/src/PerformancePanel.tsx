import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import { formatBytes, formatTimestamp, getTimezone } from "./types";
import StreamMemoryFact from "./StreamMemoryFact";
import { cachedFileComplete } from "./Preloads";
import {
  performanceApi,
  type LoadSample,
  type PerformanceSnapshot,
  type TrafficSample,
  type ResourceSample,
  type PerformanceHistory,
} from "./performanceApi";
import "./Performance.css";

type MetricKey =
  | "cpu_percent"
  | "memory_bytes"
  | "received_bytes_per_second"
  | "sent_bytes_per_second";
type MetricSeries = { key: MetricKey; label: string; secondary?: boolean };
export function metricSegments(
  samples: ResourceSample[],
  key: MetricKey,
  maximum: number,
  scope?: "cpu_scope" | "network_interface",
  maxGapSeconds = 30,
): string[] {
  const first = samples[0]?.at || 0,
    last = samples.at(-1)?.at || first;
  const segments: string[] = [];
  let current: string[] = [],
    previous: ResourceSample | undefined;
  const flush = () => {
    if (current.length) segments.push(current.join(" "));
    current = [];
    previous = undefined;
  };
  for (const sample of samples) {
    const value = sample[key];
    if (
      value === null ||
      !Number.isFinite(value) ||
      value < 0 ||
      !Number.isFinite(sample.at)
    ) {
      flush();
      continue;
    }
    if (
      previous &&
      (sample.at <= previous.at ||
        sample.at - previous.at > maxGapSeconds ||
        (scope && sample[scope] !== previous[scope]))
    )
      flush();
    current.push(
      (last === first ? 1 : (sample.at - first) / (last - first)) * 600 +
        "," +
        (112 - Math.min(1, value / Math.max(1, maximum)) * 100),
    );
    previous = sample;
  }
  flush();
  return segments;
}
export function metricCeiling(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  const unit =
    1024 ** Math.max(0, Math.floor(Math.log(value) / Math.log(1024)));
  return (
    ([1, 2, 5, 10, 20, 50, 100, 200, 500, 1024].find(
      (step) => step * unit >= value,
    ) || 1024) * unit
  );
}
function metricTime(at: number) {
  return formatTimestamp(at);
}
function MetricChart({
  title,
  samples,
  series,
  maximum,
  format,
  scope,
  current,
  note,
  windowSeconds,
  bucketSeconds = 3,
  historical = false,
}: {
  title: string;
  samples: ResourceSample[];
  series: MetricSeries[];
  maximum: number;
  format: (value: number) => string;
  scope?: "cpu_scope" | "network_interface";
  current: string;
  note: string;
  windowSeconds: number;
  bucketSeconds?: number;
  historical?: boolean;
}) {
  const [selectedAt, setSelectedAt] = useState<number | null>(null);
  const [hoverAt, setHoverAt] = useState<number | null>(null);
  useEffect(() => setHoverAt(null), [windowSeconds]);
  const inspectedAt = hoverAt ?? selectedAt;
  const found =
    inspectedAt === null
      ? -1
      : samples.findIndex((sample) => sample.at >= inspectedAt);
  const index = found < 0 ? samples.length - 1 : found;
  const selected = samples[index];
  const available = samples.some((sample) =>
    series.some(({ key }) => typeof sample[key] === "number"),
  );
  const quiet =
    available &&
    samples.every((sample) =>
      series.every(({ key }) => sample[key] === null || sample[key] === 0),
    );
  const first = samples[0]?.at || 0,
    last = samples.at(-1)?.at || first;
  const x =
    selected && last > first
      ? ((selected.at - first) / (last - first)) * 600
      : 600;
  return (
    <section
      className={
        "performance-metric-chart" +
        (series.length > 1 ? " performance-network-chart" : "")
      }
      aria-label={title}
    >
      <div className="performance-chart-heading">
        <h3>{title}</h3>
        <strong title="Current reading">
          {current}
          <small className="performance-chart-now">Now</small>
        </strong>
      </div>
      {series.length > 1 && (
        <div className="performance-chart-legend">
          {series.map(({ key, label, secondary }) => (
            <span
              key={key}
              className={
                secondary ? "chart-series-secondary" : "chart-series-primary"
              }
            >
              {label}
            </span>
          ))}
        </div>
      )}
      {(samples.length >= 2 || (historical && samples.length === 1)) &&
      available ? (
        <>
          <div className={"performance-chart-plot" + (quiet ? " is-idle" : "")}>
            <div className="performance-chart-axis" aria-hidden="true">
              {maximum > 0 && (
                <>
                  <span>{format(maximum)}</span>
                  <span>{format(maximum / 2)}</span>
                </>
              )}
              <span>{format(0)}</span>
            </div>
            <svg
              viewBox="0 0 600 130"
              preserveAspectRatio="none"
              role="img"
              aria-label={title + " over time"}
              onPointerMove={(event) => {
                if (event.pointerType !== "mouse") return;
                const bounds = event.currentTarget.getBoundingClientRect();
                const position = Math.max(
                  0,
                  Math.min(1, (event.clientX - bounds.left) / bounds.width),
                );
                const target = first + position * (last - first);
                const nearest = nearestSampleAt(samples, target);
                setHoverAt(nearest);
              }}
              onPointerLeave={() => setHoverAt(null)}
            >
              <title>{title + ": " + current}</title>
              <desc>
                {note + " Missing readings and interruptions are left as gaps."}
              </desc>
              <path
                d="M0 12H600 M0 62H600 M0 112H600"
                className="performance-chart-grid"
              />
              {series.flatMap(({ key, secondary }) =>
                metricSegments(
                  samples,
                  key,
                  maximum,
                  scope,
                  Math.max(30, bucketSeconds * 1.5),
                ).map((points, part) => (
                  <polyline
                    key={key + part}
                    points={points}
                    className={
                      secondary
                        ? "performance-plot-queued"
                        : "performance-plot-active"
                    }
                  />
                )),
              )}
              {series.flatMap(({ key, label, secondary }) =>
                samples.flatMap((sample, sampleIndex) => {
                  const value = sample[key];
                  return value === null || !Number.isFinite(value) || value < 0
                    ? []
                    : [
                        <circle
                          key={key + ":" + sampleIndex}
                          cx={
                            (last === first
                              ? 1
                              : (sample.at - first) / (last - first)) * 600
                          }
                          cy={
                            112 -
                            Math.min(1, value / Math.max(1, maximum)) * 100
                          }
                          r={2}
                          className={
                            secondary
                              ? "performance-chart-point-secondary"
                              : "performance-chart-point"
                          }
                        >
                          <title>
                            {metricTime(sample.at) +
                              " · " +
                              label +
                              " " +
                              format(value)}
                          </title>
                        </circle>,
                      ];
                }),
              )}
              {selected && (
                <path
                  d={"M" + x + " 8V116"}
                  className="performance-chart-cursor"
                />
              )}
            </svg>
            {hoverAt !== null && selected && (
              <div className="performance-chart-tooltip" aria-hidden="true">
                <time>{metricTime(selected.at)}</time>
                {series.map(({ key, label }) => (
                  <span key={key}>
                    {label}:{" "}
                    {selected[key] === null
                      ? "No reading"
                      : format(selected[key])}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="performance-chart-times">
            <span>
              {historical
                ? metricTime(first)
                : historyDuration(last - first) + " ago"}
            </span>
            <span>{historical ? metricTime(last) : "Latest reading"}</span>
          </div>
          <label className="performance-chart-scrubber">
            <span className="sr-only">{"Inspect " + title + " samples"}</span>
            <input
              type="range"
              disabled={samples.length < 2}
              min={0}
              max={samples.length - 1}
              value={Math.max(0, index)}
              onChange={(event) => {
                setHoverAt(null);
                setSelectedAt(samples[Number(event.target.value)].at);
              }}
              aria-valuetext={
                selected
                  ? metricTime(selected.at) +
                    ": " +
                    series
                      .map(
                        ({ key, label }) =>
                          label +
                          " " +
                          (selected[key] === null
                            ? "no reading"
                            : format(selected[key])),
                      )
                      .join(", ")
                  : "No reading"
              }
            />
          </label>
          <div className="performance-chart-readout">
            <time
              dateTime={
                selected
                  ? new Date(selected.at * 1000).toISOString()
                  : undefined
              }
            >
              {selected ? metricTime(selected.at) : ""}
            </time>
            {scope && selected?.[scope] && (
              <span>
                {scope === "cpu_scope"
                  ? selected.cpu_scope === "cgroup"
                    ? "Container reading"
                    : "Host reading"
                  : "Interface " + selected.network_interface}
              </span>
            )}
            {series.map(({ key, label, secondary }) => (
              <span
                key={key}
                className={
                  secondary ? "chart-series-secondary" : "chart-series-primary"
                }
              >
                {label}{" "}
                <strong>
                  {selected && selected[key] !== null
                    ? format(selected[key])
                    : "No reading"}
                </strong>
              </span>
            ))}
            {selectedAt !== null && (
              <button
                type="button"
                className="text-button"
                onClick={() => setSelectedAt(null)}
              >
                Live
              </button>
            )}
          </div>
        </>
      ) : (
        <p className="performance-chart-collecting">
          {historical
            ? "No recorded readings in this range."
            : available
              ? "Collecting the next reading…"
              : "No history yet. Readings appear as the server samples activity."}
        </p>
      )}
      <p className="performance-chart-note">
        {quiet ? "Idle during this observed window. " : ""}
        {note}
      </p>
    </section>
  );
}

const HISTORY_WINDOW_KEY = "tgdrive-performance-range";
type HistoryRange = "live" | "hour" | "day" | "week" | "month" | "custom";
const HISTORY_RANGES = {
  live: 300,
  hour: 3600,
  day: 86400,
  week: 7 * 86400,
  month: 30 * 86400,
  custom: 31 * 86400,
};
export function historyDuration(seconds: number): string {
  const rounded = Math.max(0, Math.floor(seconds));
  for (const [unit, suffix, nextUnit, nextSuffix] of [
    [86400, "day", 3600, "hr"],
    [3600, "hr", 60, "min"],
    [60, "min", 1, "sec"],
  ] as const) {
    if (rounded >= unit) {
      const count = Math.floor(rounded / unit),
        rest = Math.floor((rounded % unit) / nextUnit);
      return (
        count +
        " " +
        suffix +
        (suffix === "day" && count !== 1 ? "s" : "") +
        (rest ? " " + rest + " " + nextSuffix : "")
      );
    }
  }
  return rounded + " sec";
}
export function samplesInWindow<T extends { at: number }>(
  samples: T[],
  seconds: number,
): T[] {
  const end = samples.at(-1)?.at;
  if (end === undefined || !Number.isFinite(end)) return [];
  return samples.filter(
    (sample) =>
      Number.isFinite(sample.at) &&
      sample.at >= end - seconds &&
      sample.at <= end,
  );
}
export function nearestSampleAt<T extends { at: number }>(
  samples: T[],
  target: number,
): number | null {
  return samples.reduce<number | null>(
    (nearest, sample) =>
      Number.isFinite(sample.at) &&
      (nearest === null ||
        Math.abs(sample.at - target) < Math.abs(nearest - target))
        ? sample.at
        : nearest,
    null,
  );
}
export function customHistoryBounds(
  startText: string,
  endText: string,
  now = Date.now() / 1000,
) {
  const parse = (text: string) => {
    const value = Date.parse(text + "Z") / 1000;
    if (
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(text) ||
      !Number.isFinite(value) ||
      new Date(value * 1000).toISOString().slice(0, 16) !== text
    )
      throw new Error("Choose valid start and end dates in UTC.");
    return value;
  };
  const start = parse(startText),
    end = parse(endText);
  if (end <= start) throw new Error("End must be after start.");
  if (end - start > 31 * 86400)
    throw new Error("Choose a range of at most 31 days.");
  if (end > now + 60) throw new Error("End cannot be in the future.");
  if (start < now - 31 * 86400)
    throw new Error(
      "Recorded history is retained for 31 days. Choose a more recent start.",
    );
  return { start, end };
}
function savedHistoryRange(): HistoryRange {
  try {
    const value = window.localStorage.getItem(HISTORY_WINDOW_KEY);
    if (value && ["live", "hour", "day", "week", "month"].includes(value))
      return value as HistoryRange;
  } catch {
    /* Browser storage can be unavailable. */
  }
  return "live";
}

/** Charts show a recent continuous window, never a line across a polling gap. */
export function recentSamples<T extends { at: number }>(
  samples: T[],
  seconds = 300,
): T[] {
  const end = samples.at(-1)?.at;
  if (end === undefined || !Number.isFinite(end)) return [];
  let start = samples.length - 1;
  while (start > 0) {
    const gap = samples[start].at - samples[start - 1].at;
    if (
      !Number.isFinite(gap) ||
      gap <= 0 ||
      gap > 30 ||
      end - samples[start - 1].at > seconds
    )
      break;
    start--;
  }
  return samples.slice(start);
}

export function UsageMeter({
  label,
  used,
  total,
  detail,
  percent = false,
  unavailableLabel = "Unavailable",
}: {
  label: string;
  used: number;
  total: number;
  detail?: string;
  percent?: boolean;
  unavailableLabel?: string;
}) {
  const valid = Number.isFinite(used) && Number.isFinite(total) && total > 0;
  const bounded = valid ? Math.min(total, Math.max(0, used)) : 0;
  return (
    <div className="performance-usage">
      <div>
        <span>{label}</span>
        <strong>
          {valid
            ? percent
              ? Math.max(0, used).toFixed(1) + "%"
              : formatBytes(Math.max(0, used)) + " / " + formatBytes(total)
            : total === 0 && used === 0
              ? "Disabled"
              : unavailableLabel}
        </strong>
      </div>
      {valid && <progress aria-label={label} value={bounded} max={total} />}
      {detail && <small>{detail}</small>}
    </div>
  );
}

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
      seconds > 30 ||
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
  const [range, setRange] = useState<HistoryRange>(savedHistoryRange);
  const [customStart, setCustomStart] = useState(() =>
    new Date(Date.now() - 86400000).toISOString().slice(0, 16),
  );
  const [customEnd, setCustomEnd] = useState(() =>
    new Date().toISOString().slice(0, 16),
  );
  const [customBounds, setCustomBounds] = useState<{
    start: number;
    end: number;
  } | null>(null);
  const [customError, setCustomError] = useState("");
  const [recorded, setRecorded] = useState<PerformanceHistory | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [loadedRange, setLoadedRange] = useState("");
  const rangeKey = range === "custom" ? JSON.stringify(customBounds) : range;
  const windowSeconds =
    range === "custom" && customBounds
      ? customBounds.end - customBounds.start
      : HISTORY_RANGES[range];
  const historical = range !== "live";
  const [view, setView] = useState<"overview" | "telegram" | "server">(
    "overview",
  );
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
  useEffect(() => {
    if (range === "custom" && !customBounds) {
      setHistoryError("");
      setHistoryLoading(false);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      setHistoryLoading(true);
      setHistoryError("");
      const end =
        range === "custom" ? customBounds!.end : Math.floor(Date.now() / 1000);
      const start =
        range === "custom" ? customBounds!.start : end - HISTORY_RANGES[range];
      try {
        const data = await performanceApi.history(
          start,
          end,
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setRecorded(data);
          setLoadedRange(rangeKey);
        }
      } catch (cause) {
        if (!controller.signal.aborted) setHistoryError(errorMessage(cause));
      } finally {
        if (!controller.signal.aborted) {
          setHistoryLoading(false);
          if (range !== "custom") timer = setTimeout(poll, 60000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [range, customBounds, rangeKey, refresh]);
  const telegram = snapshot?.telegram;
  const history = recentSamples(telegram?.history || [], windowSeconds);
  const ceiling = Math.max(
    1,
    ...history.flatMap((sample) => [sample.active, sample.queued]),
  );
  const traffic = snapshot?.traffic;
  const rates = httpRates(recentSamples(traffic?.history || [], windowSeconds));
  const latestRate = rates.at(-1);
  const rateFresh =
    latestRate && Math.abs(Date.now() / 1000 - latestRate.at) <= 30;
  const sendRate =
    traffic?.download_bytes_per_second ??
    (rateFresh ? latestRate.active : null);
  const receiveRate =
    traffic?.upload_bytes_per_second ?? (rateFresh ? latestRate.queued : null);
  const downloads =
    telegram?.accounts.reduce((sum, account) => sum + account.downloads, 0) ||
    0;
  const uploads =
    telegram?.accounts.reduce((sum, account) => sum + account.uploads, 0) || 0;
  const hasTraffic = rates.some(
    (sample) => sample.active > 0 || sample.queued > 0,
  );
  const hasTelegramActivity = history.some(
    (sample) => sample.active > 0 || sample.queued > 0,
  );
  const memory = snapshot?.server.memory;
  const memoryTotal = memory?.cgroup_limit || 0;
  const memoryUsed = memory?.cgroup_current ?? NaN;
  const network = snapshot?.server.network;
  const cpu = snapshot?.server.cpu;
  const resourceHistory = historical
    ? range === "custom" && !customBounds
      ? []
      : recorded?.samples || []
    : samplesInWindow(snapshot?.server.history || [], 300);
  const networkCeiling = metricCeiling(
    Math.max(
      0,
      ...resourceHistory.flatMap((sample) => [
        sample.received_bytes_per_second || 0,
        sample.sent_bytes_per_second || 0,
      ]),
    ),
  );
  const memoryLimit = memory?.cgroup_limit || 0;
  const memoryCeiling =
    memoryLimit > 0
      ? Math.max(
          memoryLimit,
          ...resourceHistory.map((sample) => sample.memory_limit_bytes || 0),
          ...resourceHistory.map((sample) => sample.memory_bytes || 0),
        )
      : metricCeiling(
          Math.max(
            0,
            ...resourceHistory.map((sample) => sample.memory_bytes || 0),
          ),
        );
  const networkRate = (value: number | null | undefined) =>
    typeof value === "number" ? formatBytes(value) + "/s" : "Measuring…";
  const rateCeiling = Math.max(
    1,
    ...rates.flatMap((sample) => [sample.active, sample.queued]),
  );
  return (
    <div className="performance-page page-body">
      <div className="performance-section-heading">
        <div>
          <p>See current traffic, account capacity and server health.</p>
        </div>
        <div className="performance-history-controls">
          <label>
            Graph interval
            <select
              aria-label="Graph interval"
              value={range}
              onChange={(event) => {
                const value = event.target.value as HistoryRange;
                setRange(value);
                setCustomError("");
                try {
                  if (value !== "custom")
                    window.localStorage.setItem(HISTORY_WINDOW_KEY, value);
                } catch {
                  /* Keep working without storage. */
                }
              }}
            >
              <option value="live">Live (5 min)</option>
              <option value="hour">Last hour</option>
              <option value="day">Last 24 hours</option>
              <option value="week">Last 7 days</option>
              <option value="month">Last month (30 days)</option>
              <option value="custom">Custom range</option>
            </select>
          </label>
          <button
            className="secondary-button"
            onClick={() => setRefresh((value) => value + 1)}
            aria-label="Refresh Performance"
          >
            <RefreshCw size={16} />
            <span>Refresh</span>
          </button>
        </div>
      </div>
      {range === "custom" && (
        <form
          className="performance-custom-range"
          onSubmit={(event) => {
            event.preventDefault();
            try {
              setCustomBounds(customHistoryBounds(customStart, customEnd));
              setCustomError("");
            } catch (cause) {
              setCustomError(errorMessage(cause));
            }
          }}
        >
          <label>
            Start (UTC)
            <input
              type="datetime-local"
              required
              value={customStart}
              onChange={(event) => setCustomStart(event.target.value)}
            />
          </label>
          <label>
            End (UTC)
            <input
              type="datetime-local"
              required
              value={customEnd}
              onChange={(event) => setCustomEnd(event.target.value)}
            />
          </label>
          <button className="secondary-button" type="submit">
            Apply Range
          </button>
          <p className="settings-help">
            Enter UTC times. Chart timestamps use {getTimezone()}. Up to 31
            days; gaps mean no readings were recorded.
          </p>
          {customBounds && (
            <p className="settings-help">
              Applied: {formatTimestamp(customBounds.start)} –{" "}
              {formatTimestamp(customBounds.end)} ({getTimezone()})
            </p>
          )}
          <ErrorNotice message={customError} />
        </form>
      )}
      {historyLoading && historical && (
        <p className="performance-history-status" role="status">
          <Spinner /> Loading selected history…
          {recorded && loadedRange !== rangeKey
            ? " Previous range remains visible until it is ready."
            : ""}
        </p>
      )}
      {historyError && (
        <div className="performance-history-error">
          <ErrorNotice
            message={
              (historical && recorded
                ? "Showing previously loaded history. "
                : "") + historyError
            }
          />
          <button
            type="button"
            className="text-button"
            onClick={() => setRefresh((value) => value + 1)}
          >
            Retry History
          </button>
        </div>
      )}
      {loading && (
        <p className="performance-loading" role="status">
          <Spinner /> Loading server performance…
        </p>
      )}
      <ErrorNotice message={error} />
      {snapshot && telegram && (
        <>
          <dl className="performance-facts" hidden={view === "overview"}>
            <div>
              <dt>Server network received</dt>
              <dd>
                {network?.available
                  ? typeof network.received_bytes_per_second === "number"
                    ? formatBytes(network.received_bytes_per_second) + "/s"
                    : "Measuring…"
                  : "Unavailable"}
              </dd>
              <small>
                {network?.available
                  ? formatBytes(network.received_bytes) +
                    " received since monitoring started"
                  : "Network measurement unavailable"}
              </small>
            </div>
            <div>
              <dt>Server network sent</dt>
              <dd>
                {network?.available
                  ? typeof network.sent_bytes_per_second === "number"
                    ? formatBytes(network.sent_bytes_per_second) + "/s"
                    : "Measuring…"
                  : "Unavailable"}
              </dd>
              <small>
                {network?.available
                  ? formatBytes(network.sent_bytes) +
                    " sent since monitoring started"
                  : "Network measurement unavailable"}
              </small>
            </div>
            <div>
              <dt>Telegram transfers</dt>
              <dd>
                {uploads} uploading · {downloads} downloading
              </dd>
              <small>
                {telegram.queued} waiting · cached delivery does not need a bot
                slot
              </small>
            </div>
          </dl>
          <p
            className="settings-help performance-network-help"
            hidden={view === "overview"}
          >
            Network traffic includes Telegram transfers, URL downloads and
            device connections
            {network?.interface ? " on " + network.interface : ""}. It is
            separate from TGDrive file-endpoint traffic below.
          </p>
          <div
            className="performance-filters performance-view-tabs"
            role="tablist"
            aria-label="Performance views"
          >
            {(
              [
                ["overview", "Overview"],
                ["telegram", "Accounts"],
                ["server", "Resources"],
              ] as const
            ).map(([key, label], index, tabs) => (
              <button
                type="button"
                role="tab"
                id={"performance-tab-" + key}
                aria-controls={"performance-view-" + key}
                aria-selected={view === key}
                tabIndex={view === key ? 0 : -1}
                key={key}
                onClick={() => setView(key)}
                onKeyDown={(event) => {
                  const next =
                    event.key === "ArrowRight"
                      ? (index + 1) % tabs.length
                      : event.key === "ArrowLeft"
                        ? (index + tabs.length - 1) % tabs.length
                        : event.key === "Home"
                          ? 0
                          : event.key === "End"
                            ? tabs.length - 1
                            : -1;
                  if (next >= 0) {
                    event.preventDefault();
                    setView(tabs[next][0]);
                    document
                      .getElementById("performance-tab-" + tabs[next][0])
                      ?.focus();
                  }
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="performance-history-availability">
            {historical
              ? recorded && loadedRange === rangeKey
                ? `Recorded interval averages (${historyDuration(recorded.bucket_seconds)} per point). Current values marked Now stay live. ${recorded.samples.some((sample) => sample.cpu_percent !== null || sample.memory_bytes !== null || sample.received_bytes_per_second !== null || sample.sent_bytes_per_second !== null) ? `Available history begins ${formatTimestamp(recorded.earliest_at)}.` : "No readings were recorded for this range."}`
                : range === "custom" && !customBounds
                  ? "Choose dates and apply a range to inspect recorded history."
                  : "Selected range has not loaded yet."
              : resourceHistory.length > 1
                ? `Showing ${historyDuration(resourceHistory.at(-1)!.at - resourceHistory[0].at)} of collected live history; up to 5 min selected.`
                : "Collecting live history; earlier readings are not available yet."}
          </p>
          <div
            id="performance-view-overview"
            role="tabpanel"
            aria-labelledby="performance-tab-overview"
            hidden={view !== "overview"}
            tabIndex={0}
            className="performance-overview-grid"
          >
            <MetricChart
              windowSeconds={windowSeconds}
              historical={historical}
              bucketSeconds={historical ? recorded?.bucket_seconds || 60 : 3}
              title="Network traffic"
              samples={resourceHistory}
              series={[
                {
                  key: "received_bytes_per_second",
                  label: "Download to server",
                },
                {
                  key: "sent_bytes_per_second",
                  label: "Upload from server",
                  secondary: true,
                },
              ]}
              maximum={networkCeiling}
              format={(value) => formatBytes(value) + "/s"}
              scope="network_interface"
              current={
                "Download " +
                networkRate(network?.received_bytes_per_second) +
                " · Upload " +
                networkRate(network?.sent_bytes_per_second)
              }
              note={
                (network?.available
                  ? formatBytes(network.received_bytes) +
                    " received · " +
                    formatBytes(network.sent_bytes) +
                    " sent during this runtime session. "
                  : "Network readings unavailable. ") +
                "Includes Telegram, URL downloads, device uploads and viewer downloads" +
                (network?.interface ? " on " + network.interface : "") +
                "."
              }
            />
            <MetricChart
              windowSeconds={windowSeconds}
              historical={historical}
              bucketSeconds={historical ? recorded?.bucket_seconds || 60 : 3}
              title={cpu?.scope === "host" ? "Host CPU" : "Container CPU"}
              samples={resourceHistory}
              series={[{ key: "cpu_percent", label: "Usage" }]}
              maximum={100}
              format={(value) => value.toFixed(1) + "%"}
              scope="cpu_scope"
              current={
                typeof cpu?.percent === "number"
                  ? cpu.percent.toFixed(1) + "%"
                  : "Measuring…"
              }
              note={
                cpu?.capacity_cores
                  ? "100% uses the available " +
                    cpu.capacity_cores.toLocaleString() +
                    " CPU cores."
                  : "Measured CPU utilization; missing readings remain gaps."
              }
            />
            <MetricChart
              windowSeconds={windowSeconds}
              historical={historical}
              bucketSeconds={historical ? recorded?.bucket_seconds || 60 : 3}
              title="Container RAM"
              samples={resourceHistory}
              series={[{ key: "memory_bytes", label: "In use" }]}
              maximum={memoryCeiling}
              format={formatBytes}
              current={
                Number.isFinite(memoryUsed)
                  ? formatBytes(memoryUsed) +
                    (memoryLimit ? " / " + formatBytes(memoryLimit) : " in use")
                  : "Unavailable"
              }
              note={
                memoryLimit
                  ? "Current container limit: " +
                    formatBytes(memoryLimit) +
                    ". Includes all processes in this container."
                  : "No container memory limit. The graph scales to observed usage, not total capacity."
              }
            />
            <section
              className="performance-overview-status"
              aria-label="Current work and cache"
            >
              <span>
                <strong>{uploads}</strong> uploading
              </span>
              <span>
                <strong>{downloads}</strong> downloading from Telegram
              </span>
              <span>
                <strong>{telegram.queued}</strong> waiting
              </span>
              <a className="inline-link" href="/?view=preloads">
                {snapshot.cache.files.filter(cachedFileComplete).length} cached
                files ready
              </a>
            </section>
            {recorded && (
              <section
                className="performance-all-time"
                aria-labelledby="all-time-heading"
              >
                <h3 id="all-time-heading">All-time totals</h3>
                <p className="settings-help">
                  {recorded.tracked_since
                    ? `Monitored since ${formatTimestamp(recorded.tracked_since)}.`
                    : "No totals have been recorded yet."}{" "}
                  These totals survive restart and are independent of the graph
                  interval. Earlier usage is not backfilled. These layers can
                  count the same transfer; do not add them together.
                </p>
                <dl className="performance-facts">
                  {(
                    [
                      [
                        "Server network received",
                        recorded.totals.network_received_bytes,
                      ],
                      [
                        "Server network sent",
                        recorded.totals.network_sent_bytes,
                      ],
                      [
                        "Files delivered by TGDrive",
                        recorded.totals.http_download_bytes,
                      ],
                      [
                        "Device uploads to TGDrive",
                        recorded.totals.http_upload_bytes,
                      ],
                      [
                        "Telegram downloads",
                        recorded.totals.telegram_download_bytes,
                      ],
                      [
                        "Telegram uploads",
                        recorded.totals.telegram_upload_bytes,
                      ],
                    ] as const
                  ).map(([label, bytes]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>
                        {typeof bytes === "number"
                          ? formatBytes(bytes)
                          : "Not available"}
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}
            <details
              className="performance-overview-details"
              hidden={historical}
            >
              <summary>Cache, file transfers & queue history</summary>
              <div className="performance-overview-grid">
                <section
                  className="performance-section performance-resource-summary"
                  aria-labelledby="current-capacity-heading"
                >
                  <h3 id="current-capacity-heading">Current server capacity</h3>
                  <UsageMeter
                    label={cpu?.scope === "host" ? "Host CPU" : "Container CPU"}
                    used={cpu?.percent ?? NaN}
                    total={100}
                    percent
                    unavailableLabel={cpu?.scope ? "Measuring…" : "Unavailable"}
                    detail={
                      typeof cpu?.capacity_cores === "number"
                        ? "Measured usage across " +
                          cpu.capacity_cores.toLocaleString() +
                          " available CPU cores"
                        : "Collecting CPU usage samples"
                    }
                  />
                  {memoryTotal > 0 ? (
                    <UsageMeter
                      label="Container RAM"
                      used={memoryUsed}
                      total={memoryTotal}
                      detail={
                        typeof snapshot.server.process_rss_bytes === "number"
                          ? "TGDrive process: " +
                            formatBytes(snapshot.server.process_rss_bytes)
                          : "Includes all processes within this memory limit"
                      }
                    />
                  ) : (
                    <div className="performance-usage">
                      <div>
                        <span>Container RAM</span>
                        <strong>
                          {Number.isFinite(memoryUsed)
                            ? formatBytes(memoryUsed) + " in use"
                            : "Unavailable"}
                        </strong>
                      </div>
                      <small>
                        No container memory limit.{" "}
                        {typeof memory?.MemAvailable === "number"
                          ? formatBytes(memory.MemAvailable) +
                            " available on the host."
                          : "Host memory availability is unknown."}
                      </small>
                    </div>
                  )}
                  <UsageMeter
                    label="Cache filesystem"
                    used={snapshot.server.disk.used_bytes}
                    total={snapshot.server.disk.total_bytes}
                    detail={
                      formatBytes(snapshot.server.disk.free_bytes) + " free"
                    }
                  />
                  <UsageMeter
                    label="RAM cache"
                    used={snapshot.cache.memory.bytes}
                    total={snapshot.cache.memory.effective_max_bytes}
                    detail={
                      snapshot.cache.memory.effective_max_bytes === 0
                        ? "RAM cache disabled"
                        : "Retained media; separate from active stream memory"
                    }
                  />
                  <UsageMeter
                    label="Disk cache"
                    used={snapshot.cache.disk.bytes}
                    total={snapshot.cache.disk.max_bytes}
                    detail={
                      !snapshot.cache.disk.available
                        ? "Disk cache unavailable"
                        : snapshot.cache.disk.max_bytes === 0
                          ? "Disk cache disabled"
                          : "Disposable copies; originals stay in Telegram"
                    }
                  />
                </section>
                <section
                  className="performance-section performance-cache-summary"
                  aria-labelledby="ready-media-heading"
                >
                  <h3 id="ready-media-heading">Media ready to reuse</h3>
                  <p>
                    <strong>
                      {snapshot.cache.files.filter(cachedFileComplete).length}
                    </strong>{" "}
                    fully cached files ·{" "}
                    {
                      snapshot.cache.files.filter(
                        (file) => !cachedFileComplete(file),
                      ).length
                    }{" "}
                    partially cached
                  </p>
                  <p className="settings-help">
                    Ready ranges are served directly from the server. Files can
                    be evicted when space is needed.
                  </p>
                  <a className="inline-link" href="/?view=preloads">
                    View cached files
                  </a>
                </section>
                {traffic && (
                  <section
                    className="performance-section"
                    aria-labelledby="http-traffic-heading"
                  >
                    <h3 id="http-traffic-heading">TGDrive file traffic</h3>
                    <dl className="performance-facts">
                      <div>
                        <dt>Downloads to viewers</dt>
                        <dd>
                          {typeof sendRate === "number"
                            ? formatBytes(sendRate) + "/s"
                            : "Measuring…"}
                        </dd>
                        <small>Includes cached file delivery</small>
                      </div>
                      <div>
                        <dt>Uploads from devices</dt>
                        <dd>
                          {typeof receiveRate === "number"
                            ? formatBytes(receiveRate) + "/s"
                            : "Measuring…"}
                        </dd>
                        <small>
                          Data arriving through TGDrive's upload endpoints
                        </small>
                      </div>
                      <div>
                        <dt>Total file bytes delivered</dt>
                        <dd>{formatBytes(traffic.download_bytes)}</dd>
                        <small>File/download and receipt response bodies</small>
                      </div>
                      <div>
                        <dt>Total device upload bytes</dt>
                        <dd>{formatBytes(traffic.upload_bytes)}</dd>
                        <small>Upload/receipt request bodies</small>
                      </div>
                      <div>
                        <dt>Tracked requests</dt>
                        <dd>
                          {traffic.requests.toLocaleString()} total ·{" "}
                          {traffic.active} active
                        </dd>
                        <small>
                          {traffic.errors.toLocaleString()} errors;
                          process-local counters
                        </small>
                      </div>
                    </dl>
                    {rates.length >= 2 && hasTraffic ? (
                      <>
                        <div className="performance-plot-legend">
                          <span>Sent per second</span>
                          <span>Received per second</span>
                        </div>
                        <p className="settings-help">
                          Scale 0–{formatBytes(rateCeiling)}/s · measured
                          differences between recent samples
                        </p>
                        <svg
                          className="performance-load-plot"
                          role="img"
                          aria-label="Measured HTTP send and receive throughput"
                          viewBox="0 0 600 130"
                          preserveAspectRatio="none"
                        >
                          <path
                            d="M0 120H600"
                            className="performance-plot-axis"
                          />
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
                        {rates.length < 2
                          ? "Measuring transfer speed. Samples update every few seconds."
                          : "No transfer traffic in the recent sample window."}
                      </p>
                    )}
                    <details className="performance-detail-note">
                      <summary>What these traffic counters include</summary>
                      <p className="settings-help">
                        Tracks /file and resumable-upload HTTP bodies, including
                        cached responses. Excludes settings polls and assets.
                        These counters reset on restart and are not physical
                        network-interface utilization.
                      </p>
                    </details>
                  </section>
                )}
                <section
                  className="performance-section"
                  aria-labelledby="recent-load-heading"
                >
                  <h3 id="recent-load-heading">Recent Telegram load</h3>
                  {history.length >= 2 && hasTelegramActivity ? (
                    <>
                      <p className="settings-help">
                        0–{ceiling} simultaneous operations ·{" "}
                        {historyDuration(history.at(-1)!.at - history[0].at)} of
                        sampled activity
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
                        <path
                          d="M0 120H600"
                          className="performance-plot-axis"
                        />
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
                      {history.length < 2
                        ? "Collecting recent activity. Transfer counts are available above."
                        : "Telegram accounts are idle. Cached files can still be served directly."}
                    </p>
                  )}
                </section>
              </div>
            </details>
          </div>
          <div
            id="performance-view-telegram"
            role="tabpanel"
            aria-labelledby="performance-tab-telegram"
            hidden={view !== "telegram"}
            tabIndex={0}
          >
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
                            <small>
                              Combined limit {account.combined_limit}
                            </small>
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
                Demand reads have priority over background preparation.
                Estimates change with workload and provider cooldowns.
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
          </div>
          <div
            id="performance-view-server"
            role="tabpanel"
            aria-labelledby="performance-tab-server"
            hidden={view !== "server"}
            tabIndex={0}
          >
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
                  <dt>
                    {cpu?.scope === "host"
                      ? "Host CPU utilization"
                      : "Container CPU utilization"}
                  </dt>
                  <dd>
                    {typeof cpu?.percent === "number"
                      ? cpu.percent.toFixed(1) + "%"
                      : "Measuring…"}
                  </dd>
                  <small>
                    {cpu?.capacity_cores
                      ? cpu.capacity_cores + " available CPU cores"
                      : "Current sampled CPU use"}
                  </small>
                </div>
                <div>
                  <dt>TGDrive process memory</dt>
                  <dd>
                    {typeof snapshot.server.process_rss_bytes === "number"
                      ? formatBytes(snapshot.server.process_rss_bytes)
                      : "Unavailable"}
                  </dd>
                  <small>
                    Current resident memory · peak{" "}
                    {formatBytes(snapshot.server.process_peak_rss_bytes)}
                  </small>
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
                  <dt>RAM cache lookups</dt>
                  <dd>
                    {snapshot.cache.memory.hits.toLocaleString()} hits ·{" "}
                    {snapshot.cache.memory.misses.toLocaleString()} misses
                  </dd>
                  <small>
                    Block lookups, including preparation; not a bandwidth-saving
                    ratio
                  </small>
                </div>
                <StreamMemoryFact scratch={snapshot.cache.scratch} />
                <div>
                  <dt>Catalog</dt>
                  <dd>
                    {snapshot.drive.files.toLocaleString()} files ·{" "}
                    {formatBytes(snapshot.drive.total_bytes)}
                  </dd>
                  <small>
                    {snapshot.drive.folders.toLocaleString()} folders; file
                    sizes stored in Telegram
                  </small>
                </div>
                <div>
                  <dt>Upload staging budget</dt>
                  <dd>{formatBytes(snapshot.server.staging_budget_bytes)}</dd>
                  <small>Separate from disposable media cache</small>
                </div>
              </dl>
              <details className="performance-detail-note">
                <summary>Resource measurement details</summary>
                <p className="settings-help">
                  {network?.scope || "Network interface scope unavailable."}{" "}
                  {snapshot.server.load_average.length
                    ? "Host load averages (1 / 5 / 15 minutes): " +
                      snapshot.server.load_average
                        .map((value) => value.toFixed(2))
                        .join(" / ") +
                      ". These are not CPU percentages."
                    : "Host load averages unavailable."}
                </p>
              </details>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
