import { Paper, UnstyledButton } from "@mantine/core";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/react/tabs";
import {
    useCallback,
    useEffect,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { api, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import {
    formatTimestamp,
    getTimezone,
    type ManagementStats,
} from "../../shared/types";
import StreamMemoryFact from "./StreamMemoryFact";
import PerformanceOverview, {
    NetworkMeasurementHelp,
    NetworkReadings,
    useCurrentPerformance,
} from "./PerformanceOverview";
import SettingHelp from "../../shared/SettingHelp";
import { measuredNumber, megabits } from "./performanceReadings";
import { cachedFileComplete } from "./Preloads";
import {
    performanceApi,
    resourceBytes as formatBytes,
    type LoadSample,
    type PerformanceSnapshot,
    type TrafficSample,
    type ResourceSample,
    type PerformanceHistory,
} from "./performanceApi";
import "./Performance.css";
import "./PerformanceAccounts.css";

const TUNING_LABELS: Record<string, string> = {
    cache_memory_bytes: "Memory cache",
    cache_disk_bytes: "Disk cache",
    cache_min_free_bytes: "Cache free-space floor",
    staging_budget_bytes: "Upload staging budget",
    max_file_size: "Largest accepted file",
    min_free_bytes: "Upload free-space reserve",
    telegram_uploads_per_bot: "Uploads per bot",
    telegram_upload_pipeline: "Upload pipeline",
    telegram_request_limit: "Requests per account",
    telegram_media_per_bot: "Media operations per account",
    remote_download_concurrency: "Full-speed URL downloads",
    preload_max_jobs: "Concurrent preloads",
};

const TUNING_CATEGORIES = [
    {
        title: "Cache",
        keys: [
            "cache_memory_bytes",
            "cache_disk_bytes",
            "cache_min_free_bytes",
        ],
    },
    {
        title: "Upload staging",
        keys: ["staging_budget_bytes", "max_file_size", "min_free_bytes"],
    },
    {
        title: "Concurrent operations",
        keys: [
            "telegram_uploads_per_bot",
            "telegram_upload_pipeline",
            "telegram_request_limit",
            "telegram_media_per_bot",
            "remote_download_concurrency",
            "preload_max_jobs",
        ],
    },
] as const;

const TUNING_DESCRIPTIONS: Record<string, string> = {
    cache_memory_bytes: "Retained media blocks in TGDrive process memory",
    cache_disk_bytes:
        "Local disk reserved for preloaded files and media chunks",
    cache_min_free_bytes:
        "Safety margin preserved before cache eviction triggers",
    staging_budget_bytes: "Space to assemble and encrypt outbound uploads",
    max_file_size: "Largest single file supported by current staging capacity",
    min_free_bytes: "Unassigned disk floor protected from uploads",
    telegram_uploads_per_bot: "Parallel file uploads per bot connection",
    telegram_upload_pipeline: "Simultaneous chunk delivery pipelines",
    telegram_request_limit: "Safe API requests allowed per Telegram account",
    telegram_media_per_bot: "Concurrent Telegram media operations per account",
    remote_download_concurrency:
        "Full-speed URL imports running simultaneously",
    preload_max_jobs: "Background preload tasks running simultaneously",
};

function AutomaticTuning({
    tuning,
    current,
    serverAt,
}: {
    tuning: PerformanceSnapshot["tuning"];
    current: boolean;
    serverAt: number;
}) {
    const groups = tuning
        ? TUNING_CATEGORIES.map(({ title, keys }) => ({
              title,
              keys: keys.filter((key) => key in tuning.values),
          })).filter(({ keys }) => keys.length > 0)
        : [];
    const sampledAt = tuning?.measurements?.sampled_at;
    const stale =
        !current ||
        (sampledAt != null &&
            serverAt - sampledAt > 3 * (tuning?.interval_seconds ?? 5));
    const hasValues = tuning && groups.length > 0;

    return (
        <section
            className="performance-section tuning-section"
            aria-labelledby="automatic-tuning-heading"
        >
            <div className="performance-section-title">
                <h3 id="automatic-tuning-heading">Automatic tuning</h3>
                {hasValues && (
                    <span className="performance-summary-value">
                        {stale
                            ? "Last applied values"
                            : tuning.example
                              ? "Example"
                              : tuning.running
                                ? "Active"
                                : "Paused"}
                    </span>
                )}
            </div>
            <p className="tuning-intro">
                {!hasValues
                    ? "Waiting for a tuning report. You can review your configured limits above."
                    : tuning.example
                      ? "Sample values calculated from example server resources."
                      : stale
                        ? "Tuning readings are out of date. Showing the last applied limits."
                        : "Effective limits for cache, uploads, and concurrent work."}
            </p>
            {hasValues && (
                <div className="tuning-groups">
                    {groups.map((group) => (
                        <div key={group.title} className="tuning-group">
                            <div className="tuning-group-header">
                                <h4>{group.title}</h4>
                            </div>
                            <dl className="tuning-facts">
                                {group.keys.map((key) => {
                                    const label = TUNING_LABELS[key];
                                    const val =
                                        key.endsWith("_bytes") ||
                                        key === "max_file_size"
                                            ? formatBytes(tuning.values[key])
                                            : tuning.values[
                                                  key
                                              ].toLocaleString();
                                    const reason = tuning.reasons[key]?.replace(
                                        / \(\d+(?:\.\d+)?(?: GiB)?\)$/,
                                        "",
                                    );
                                    const detail =
                                        reason === "startup ceiling"
                                            ? null
                                            : reason ||
                                              TUNING_DESCRIPTIONS[key];
                                    return (
                                        <div
                                            key={key}
                                            className="tuning-fact-item"
                                        >
                                            <dt>{label}</dt>
                                            <dd>{val}</dd>
                                            {detail && (
                                                <dd className="tuning-fact-detail">
                                                    {detail}
                                                </dd>
                                            )}
                                        </div>
                                    );
                                })}
                            </dl>
                        </div>
                    ))}
                </div>
            )}
        </section>
    );
}

type MetricKey =
    | "cpu_percent"
    | "memory_bytes"
    | "received_bytes_per_second"
    | "sent_bytes_per_second";
type MetricSeries = {
    key: MetricKey;
    label: string;
    secondary?: boolean;
};
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
    bucketSeconds = 3,
    historical = false,
    summary,
    headingHelp,
}: {
    title: string;
    samples: ResourceSample[];
    series: MetricSeries[];
    maximum: number;
    format: (value: number) => string;
    scope?: "cpu_scope" | "network_interface";
    current: string;
    note: string;
    bucketSeconds?: number;
    historical?: boolean;
    summary?: ReactNode;
    headingHelp?: ReactNode;
}) {
    const [selectedAt, setSelectedAt] = useState<number | null>(null);
    const [hoverAt, setHoverAt] = useState<number | null>(null);
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
            series.every(
                ({ key }) => sample[key] === null || sample[key] === 0,
            ),
        );
    const first = samples[0]?.at || 0,
        last = samples.at(-1)?.at || first;
    const x =
        selected && last > first
            ? ((selected.at - first) / (last - first)) * 600
            : 600;
    const sampleTimeAt = (clientX: number, plot: HTMLElement) => {
        const { left, width } = plot.getBoundingClientRect();
        const fraction = Math.max(0, Math.min(1, (clientX - left) / width));
        return nearestSampleAt(samples, first + fraction * (last - first));
    };
    const readingText =
        inspectedAt !== null &&
        selected &&
        series
            .map(
                ({ key, label }) =>
                    label +
                    " " +
                    (selected[key] == null
                        ? "no reading"
                        : format(selected[key])),
            )
            .join(", ");
    return (
        <Paper
            component="section"
            className={
                "performance-metric-chart" +
                (series.length > 1 ? " performance-network-chart" : "")
            }
            aria-label={title}
            data-metric={series[0]?.key}
        >
            <div className="performance-chart-heading">
                <div className="performance-metric-title">
                    <h3>{title}</h3>
                    {headingHelp ?? (
                        <SettingHelp label={title}>
                            <p>{note}</p>
                        </SettingHelp>
                    )}
                </div>
                {!historical && !summary && (
                    <strong title="Current reading">
                        {current}
                        <small className="performance-chart-now">Now</small>
                    </strong>
                )}
            </div>
            {summary}
            {(samples.length >= 2 || (historical && samples.length === 1)) &&
            available ? (
                <>
                    <div
                        className={
                            "performance-chart-plot" + (quiet ? " is-idle" : "")
                        }
                    >
                        <div
                            className="performance-chart-axis"
                            aria-hidden="true"
                        >
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
                        >
                            <desc>
                                {note +
                                    " Missing readings and interruptions are left as gaps."}
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
                                ).map((points, part) => {
                                    if (points.includes(" ")) {
                                        return (
                                            <polyline
                                                key={key + part}
                                                points={points}
                                                className={
                                                    secondary
                                                        ? "performance-plot-queued"
                                                        : "performance-plot-active"
                                                }
                                            />
                                        );
                                    }
                                    // A one-point segment has no visible stroke.
                                    const [cx, cy] = points.split(",");
                                    return (
                                        <circle
                                            key={key + part}
                                            cx={cx}
                                            cy={cy}
                                            r={2}
                                            className={
                                                secondary
                                                    ? "performance-chart-point-secondary"
                                                    : "performance-chart-point"
                                            }
                                        />
                                    );
                                }),
                            )}
                            {selected && inspectedAt !== null && (
                                <path
                                    d={"M" + x + " 8V116"}
                                    className="performance-chart-cursor"
                                />
                            )}
                        </svg>
                        <UnstyledButton
                            type="button"
                            className="performance-chart-inspect"
                            aria-label={"Inspect " + title + " samples"}
                            aria-description="Use Left and Right to move between readings, Home or End to jump, and Escape to clear."
                            onPointerMove={(event) => {
                                if (event.pointerType === "mouse")
                                    setHoverAt(
                                        sampleTimeAt(
                                            event.clientX,
                                            event.currentTarget,
                                        ),
                                    );
                            }}
                            onPointerLeave={() => setHoverAt(null)}
                            onBlur={() => setSelectedAt(null)}
                            onClick={(event) => {
                                event.currentTarget.focus({
                                    preventScroll: true,
                                });
                                const at =
                                    event.detail === 0
                                        ? last
                                        : sampleTimeAt(
                                              event.clientX,
                                              event.currentTarget,
                                          );
                                setSelectedAt(selectedAt === at ? null : at);
                                setHoverAt(null);
                            }}
                            onKeyDown={(event) => {
                                const key = event.key;
                                if (key === "Escape" && inspectedAt !== null) {
                                    event.stopPropagation();
                                    setSelectedAt(null);
                                    setHoverAt(null);
                                    return;
                                }
                                if (
                                    key !== "ArrowLeft" &&
                                    key !== "ArrowRight" &&
                                    key !== "Home" &&
                                    key !== "End"
                                )
                                    return;
                                event.preventDefault();
                                const next =
                                    key === "Home"
                                        ? 0
                                        : key === "End"
                                          ? samples.length - 1
                                          : Math.max(
                                                0,
                                                Math.min(
                                                    samples.length - 1,
                                                    index +
                                                        (key === "ArrowLeft"
                                                            ? -1
                                                            : 1),
                                                ),
                                            );
                                setSelectedAt(samples[next].at);
                                setHoverAt(null);
                            }}
                        />
                        <span className="sr-only" role="status">
                            {selectedAt !== null && hoverAt === null && selected
                                ? metricTime(selected.at) + ", " + readingText
                                : ""}
                        </span>
                        {selected && inspectedAt !== null && (
                            <div
                                className="performance-chart-tooltip"
                                aria-hidden="true"
                                data-side={x < 300 ? "end" : "start"}
                            >
                                <time>{metricTime(selected.at)}</time>
                                {scope && selected[scope] && (
                                    <span className="performance-chart-tooltip-scope">
                                        {scope === "cpu_scope"
                                            ? selected.cpu_scope === "cgroup"
                                                ? "Container reading"
                                                : "System reading"
                                            : "Interface " +
                                              selected.network_interface}
                                    </span>
                                )}
                                {series.map(({ key, label, secondary }) => (
                                    <div
                                        key={key}
                                        className="performance-chart-tooltip-row"
                                    >
                                        <span
                                            className={
                                                secondary
                                                    ? "chart-series-secondary"
                                                    : "chart-series-primary"
                                            }
                                        >
                                            {label}
                                        </span>
                                        <strong>
                                            {selected[key] == null
                                                ? "No reading"
                                                : format(selected[key])}
                                        </strong>
                                    </div>
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
                        <span>
                            {historical ? metricTime(last) : "Latest reading"}
                        </span>
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
            {quiet && (
                <p className="performance-chart-idle">
                    Idle during this observed window.
                </p>
            )}
        </Paper>
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
                            : formatBytes(Math.max(0, used)) +
                              " / " +
                              formatBytes(total)
                        : total === 0 && used === 0
                          ? "Disabled"
                          : unavailableLabel}
                </strong>
            </div>
            {valid && (
                <progress aria-label={label} value={bounded} max={total} />
            )}
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
            active:
                (current.download_bytes - previous.download_bytes) / seconds,
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
const LIVE_POLL_MS = 5000;
const HISTORY_POLL_MS = 60000;

function usePerformancePoll(
    load: (signal: AbortSignal) => Promise<boolean>,
    interval: number | null,
    enabled = true,
) {
    useEffect(() => {
        if (!enabled) return;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        let pending = false;
        const poll = async () => {
            if (document.hidden || pending) return;
            pending = true;
            const loaded = await load(controller.signal);
            pending = false;
            const delay = loaded ? interval : LIVE_POLL_MS;
            if (
                !controller.signal.aborted &&
                !document.hidden &&
                delay !== null
            )
                timer = setTimeout(poll, delay);
        };
        const onVisibility = () => {
            clearTimeout(timer);
            void poll();
        };
        document.addEventListener("visibilitychange", onVisibility);
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
            document.removeEventListener("visibilitychange", onVisibility);
        };
    }, [enabled, interval, load]);
}

export default function PerformancePanel({
    maxFileSizeBytes,
}: {
    maxFileSizeBytes?: number;
} = {}) {
    const [snapshot, setSnapshot] = useState<PerformanceSnapshot | null>(null);
    const [error, setError] = useState("");
    const [usage, setUsage] = useState<ManagementStats | null>(null);
    const [usageError, setUsageError] = useState("");
    const [usageUpdatedAt, setUsageUpdatedAt] = useState<number | null>(null);
    const [loading, setLoading] = useState(true);
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
    const [view, setView] = useState("server");
    const resourcesTab = useRef<HTMLButtonElement>(null);
    const accountsTab = useRef<HTMLButtonElement>(null);
    const current = useCurrentPerformance(snapshot, error);
    const loadSnapshot = useCallback(async (signal: AbortSignal) => {
        try {
            const value = await performanceApi.snapshot(signal);
            if (signal.aborted) return false;
            setSnapshot(value);
            setError("");
            return true;
        } catch (cause) {
            if (!signal.aborted) setError(errorMessage(cause));
            return false;
        } finally {
            if (!signal.aborted) setLoading(false);
        }
    }, []);
    const loadHistory = useCallback(
        async (signal: AbortSignal) => {
            setHistoryLoading(true);
            const end =
                range === "custom"
                    ? customBounds!.end
                    : Math.floor(Date.now() / 1000);
            const start =
                range === "custom"
                    ? customBounds!.start
                    : end - HISTORY_RANGES[range];
            try {
                const data = await performanceApi.history(start, end, signal);
                if (signal.aborted) return false;
                setRecorded(data);
                setLoadedRange(rangeKey);
                setHistoryError("");
                return true;
            } catch (cause) {
                if (!signal.aborted) setHistoryError(errorMessage(cause));
                return false;
            } finally {
                if (!signal.aborted) setHistoryLoading(false);
            }
        },
        [range, customBounds, rangeKey],
    );
    const loadUsage = useCallback(async (signal: AbortSignal) => {
        try {
            const value = await api.settings(signal);
            if (signal.aborted) return false;
            setUsage(value.stats);
            setUsageUpdatedAt(Date.now() / 1000);
            setUsageError("");
            return true;
        } catch (cause) {
            if (!signal.aborted)
                setUsageError(
                    "Upload storage readings are unavailable. " +
                        errorMessage(cause),
                );
            return false;
        }
    }, []);
    usePerformancePoll(loadSnapshot, LIVE_POLL_MS);
    usePerformancePoll(loadUsage, LIVE_POLL_MS, view === "storage");
    usePerformancePoll(
        loadHistory,
        range === "custom" ? null : HISTORY_POLL_MS,
        range !== "custom" || customBounds !== null,
    );
    const telegram = snapshot?.telegram;
    const history = recentSamples(telegram?.history || [], 300);
    const ceiling = Math.max(
        1,
        ...history.flatMap((sample) => [sample.active, sample.queued]),
    );
    const traffic = snapshot?.traffic;
    const rates = httpRates(
        recentSamples(traffic?.history || [], windowSeconds),
    );
    const latestRate = rates.at(-1);
    const rateFresh =
        latestRate && Math.abs(Date.now() / 1000 - latestRate.at) <= 30;
    const sendRate =
        traffic?.download_bytes_per_second ??
        (rateFresh ? latestRate.active : null);
    const receiveRate =
        traffic?.upload_bytes_per_second ??
        (rateFresh ? latestRate.queued : null);
    const downloads =
        telegram?.accounts.reduce(
            (sum, account) => sum + account.downloads,
            0,
        ) || 0;
    const uploads =
        telegram?.accounts.reduce((sum, account) => sum + account.uploads, 0) ||
        0;
    const hasTelegramActivity = history.some(
        (sample) => sample.active > 0 || sample.queued > 0,
    );
    const memory = snapshot?.server.memory;
    const diskCapacity =
        snapshot?.resource_summary?.storage.data ??
        snapshot?.server.disk.capacity;
    const backingFree =
        diskCapacity?.backing.configured &&
        diskCapacity.backing.available &&
        diskCapacity.effective.available
            ? measuredNumber(diskCapacity.effective.free_bytes)
            : null;
    const memoryUsed =
        snapshot?.resource_summary?.memory.used_bytes ??
        memory?.cgroup_current ??
        NaN;
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
    const resourceSummary = snapshot?.resource_summary;
    const memoryLimit =
        resourceSummary?.memory.scope === "host"
            ? (resourceSummary.memory.environment_total_bytes ??
              memory?.MemTotal ??
              0)
            : resourceSummary?.memory.scope === "cgroup"
              ? (resourceSummary.memory.cgroup_limit_bytes ?? 0)
              : 0;
    const stagingCapacity = usage?.disk_capacity ?? diskCapacity;
    const stagingFree =
        stagingCapacity?.backing.configured &&
        stagingCapacity.backing.available &&
        stagingCapacity.effective.available
            ? measuredNumber(stagingCapacity.effective.free_bytes)
            : null;
    const memoryCeiling =
        memoryLimit > 0
            ? Math.max(
                  memoryLimit,
                  ...resourceHistory.map(
                      (sample) => sample.memory_limit_bytes || 0,
                  ),
                  ...resourceHistory.map((sample) => sample.memory_bytes || 0),
              )
            : metricCeiling(
                  Math.max(
                      0,
                      ...resourceHistory.map(
                          (sample) => sample.memory_bytes || 0,
                      ),
                  ),
              );
    const trafficHistory = traffic?.history || [];
    const recentErrors =
        trafficHistory.length >= 2
            ? Math.max(
                  0,
                  trafficHistory.at(-1)!.errors - trafficHistory[0].errors,
              )
            : null;
    const waiting =
        telegram?.accounts.filter(
            (account) =>
                Math.max(
                    account.cooldown_seconds,
                    account.requests?.cooldown_seconds || 0,
                ) > 0,
        ).length || 0;
    const accountCount = telegram?.accounts.length || 0;
    const queueFull =
        !!telegram &&
        telegram.max_queue > 0 &&
        telegram.queued >= telegram.max_queue * 0.8;
    const health: "ok" | "degraded" | "failing" =
        error || !accountCount || waiting === accountCount
            ? "failing"
            : recentErrors || waiting || queueFull
              ? "degraded"
              : "ok";
    const healthDetail = [
        error
            ? "Latest reading failed"
            : !accountCount
              ? "No Telegram accounts reporting"
              : waiting
                ? waiting +
                  " of " +
                  accountCount +
                  (accountCount === 1 ? " account" : " accounts") +
                  " in a Telegram flood wait"
                : "All Telegram accounts ready",
        recentErrors === null
            ? "recent request error history unavailable"
            : recentErrors
              ? recentErrors +
                " failed " +
                (recentErrors === 1 ? "request" : "requests") +
                " in the last few minutes"
              : "no recent failed requests",
        queueFull ? "Telegram queue nearly full" : "",
        traffic ? traffic.errors.toLocaleString() + " failed this session" : "",
    ]
        .filter(Boolean)
        .join(" · ");
    const networkRate = (value: number | null | undefined) => megabits(value);
    return (
        <Tabs
            className="performance-page page-body"
            id="performance"
            value={view}
            onValueChange={(value) => {
                if (value) setView(value);
            }}
        >
            <div className="performance-section-heading performance-toolbar">
                <TabsList
                    className="performance-view-tabs"
                    aria-label="Performance views"
                >
                    <TabsTab value="server">Overview</TabsTab>
                    <TabsTab value="storage" ref={resourcesTab}>
                        Server resources
                    </TabsTab>
                    <TabsTab value="tuning">Tuning</TabsTab>
                    <TabsTab value="telegram" ref={accountsTab}>
                        Accounts
                    </TabsTab>
                </TabsList>
                {snapshot && (
                    <p className="performance-update" data-stale={!current}>
                        {current ? "Updates every 5s" : "Last loaded snapshot"}{" "}
                        ·{" "}
                        {formatTimestamp(
                            snapshot.server.sampled_at ?? snapshot.server.at,
                        )}
                    </p>
                )}
            </div>
            {loading && (
                <p className="performance-loading" role="status">
                    <Spinner /> Loading server performance…
                </p>
            )}
            <ErrorNotice
                message={
                    error && snapshot
                        ? "Showing the last loaded snapshot. " + error
                        : error
                }
            />
            {snapshot && telegram && (
                <>
                    <TabsPanel
                        value="telegram"
                        keepMounted
                        className="performance-accounts-grid"
                    >
                        <section
                            className="performance-health"
                            data-health={health}
                            aria-label="Account and request status"
                        >
                            <strong>
                                {health === "ok"
                                    ? "Accounts ready"
                                    : health === "degraded"
                                      ? "Waits or request errors observed"
                                      : error || !accountCount
                                        ? "Accounts or readings unavailable"
                                        : "All accounts waiting"}
                            </strong>
                            <span>{healthDetail}</span>
                        </section>
                        <section
                            className="performance-section performance-account-section performance-account-capacity"
                            aria-labelledby="account-load-heading"
                        >
                            <div className="performance-account-heading">
                                <h3 id="account-load-heading">
                                    Account capacity
                                </h3>
                                <section
                                    className="performance-overview-status"
                                    aria-label="Live Telegram activity"
                                >
                                    <span>
                                        <strong>{uploads}</strong> uploading
                                    </span>
                                    <span>
                                        <strong>{downloads}</strong> downloading
                                        from Telegram
                                    </span>
                                    <span>
                                        <strong>{telegram.queued}</strong>{" "}
                                        waiting
                                    </span>
                                </section>
                            </div>

                            {telegram.accounts.length ? (
                                <div className="performance-table-wrap performance-account-list">
                                    <table
                                        className="performance-table"
                                        role="table"
                                        aria-labelledby="account-load-heading"
                                    >
                                        <thead role="rowgroup">
                                            <tr role="row">
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Account
                                                </th>
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Provider waits
                                                </th>
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Media operations
                                                </th>
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Part requests
                                                </th>
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Origin bytes
                                                </th>
                                                <th
                                                    scope="col"
                                                    role="columnheader"
                                                >
                                                    Pacing
                                                </th>
                                            </tr>
                                        </thead>
                                        <tbody role="rowgroup">
                                            {telegram.accounts.map(
                                                (account) => (
                                                    <tr
                                                        key={account.name}
                                                        role="row"
                                                    >
                                                        <th
                                                            scope="row"
                                                            role="rowheader"
                                                        >
                                                            {account.name}
                                                            <small>
                                                                {account.kind ===
                                                                "user"
                                                                    ? "Premium account"
                                                                    : "Storage bot"}
                                                            </small>
                                                        </th>
                                                        <td
                                                            role="cell"
                                                            className="performance-account-waits"
                                                        >
                                                            <span
                                                                className="performance-account-label"
                                                                aria-hidden="true"
                                                            >
                                                                Provider waits
                                                            </span>
                                                            {Math.max(
                                                                account.cooldown_seconds,
                                                                account.requests
                                                                    ?.cooldown_seconds ||
                                                                    0,
                                                            ) > 0
                                                                ? queueWait(
                                                                      Math.max(
                                                                          account.cooldown_seconds,
                                                                          account
                                                                              .requests
                                                                              ?.cooldown_seconds ||
                                                                              0,
                                                                      ),
                                                                  )
                                                                : "No pause reported"}
                                                            <small>
                                                                {account.requests
                                                                    ? account
                                                                          .requests
                                                                          .floods +
                                                                      " waits observed"
                                                                    : ""}
                                                            </small>
                                                        </td>
                                                        <td role="cell">
                                                            <span
                                                                className="performance-account-label"
                                                                aria-hidden="true"
                                                            >
                                                                Media operations
                                                            </span>
                                                            {account.uploads}{" "}
                                                            upload ·{" "}
                                                            {account.downloads}{" "}
                                                            download
                                                            <small>
                                                                Combined limit{" "}
                                                                {
                                                                    account.combined_limit
                                                                }
                                                            </small>
                                                            {account.combined_limit >
                                                                0 && (
                                                                <meter
                                                                    className="performance-account-meter"
                                                                    min={0}
                                                                    max={
                                                                        account.combined_limit
                                                                    }
                                                                    value={
                                                                        account.uploads +
                                                                        account.downloads
                                                                    }
                                                                    aria-label={
                                                                        "Media operations for " +
                                                                        account.name
                                                                    }
                                                                    aria-valuetext={`${account.uploads + account.downloads} active of ${account.combined_limit} configured`}
                                                                />
                                                            )}
                                                        </td>
                                                        <td role="cell">
                                                            <span
                                                                className="performance-account-label"
                                                                aria-hidden="true"
                                                            >
                                                                Part requests
                                                            </span>
                                                            {account.requests
                                                                ? account
                                                                      .requests
                                                                      .active +
                                                                  " active / " +
                                                                  account
                                                                      .requests
                                                                      .effective_limit +
                                                                  " limit"
                                                                : "Unavailable"}
                                                            <small>
                                                                {account.requests
                                                                    ? account
                                                                          .requests
                                                                          .queued +
                                                                      " requests waiting"
                                                                    : "No request sample"}
                                                            </small>
                                                            {account.requests &&
                                                                account.requests
                                                                    .effective_limit >
                                                                    0 && (
                                                                    <meter
                                                                        className="performance-account-meter"
                                                                        min={0}
                                                                        max={
                                                                            account
                                                                                .requests
                                                                                .effective_limit
                                                                        }
                                                                        value={
                                                                            account
                                                                                .requests
                                                                                .active
                                                                        }
                                                                        aria-label={
                                                                            "Part requests for " +
                                                                            account.name
                                                                        }
                                                                        aria-valuetext={`${account.requests.active} active of ${account.requests.effective_limit} effective limit`}
                                                                    />
                                                                )}
                                                        </td>
                                                        <td role="cell">
                                                            <span
                                                                className="performance-account-label"
                                                                aria-hidden="true"
                                                            >
                                                                Origin bytes
                                                            </span>
                                                            {typeof account
                                                                .requests
                                                                ?.upload_bytes ===
                                                            "number" ? (
                                                                <>
                                                                    {formatBytes(
                                                                        account
                                                                            .requests
                                                                            .upload_bytes,
                                                                    )}{" "}
                                                                    uploaded
                                                                    <small>
                                                                        {typeof account
                                                                            .requests
                                                                            .download_bytes ===
                                                                        "number"
                                                                            ? formatBytes(
                                                                                  account
                                                                                      .requests
                                                                                      .download_bytes,
                                                                              )
                                                                            : "Unavailable"}{" "}
                                                                        downloaded
                                                                    </small>
                                                                </>
                                                            ) : (
                                                                "Unavailable"
                                                            )}
                                                        </td>
                                                        <td role="cell">
                                                            <span
                                                                className="performance-account-label"
                                                                aria-hidden="true"
                                                            >
                                                                Pacing
                                                            </span>
                                                            {account.requests
                                                                ? account.requests.effective_parts_per_second.toFixed(
                                                                      1,
                                                                  ) +
                                                                  " / second"
                                                                : "—"}
                                                            <small>
                                                                {account.requests
                                                                    ? "Configured ceiling " +
                                                                      account
                                                                          .requests
                                                                          .configured_parts_per_second
                                                                    : ""}
                                                            </small>
                                                        </td>
                                                    </tr>
                                                ),
                                            )}
                                        </tbody>
                                    </table>
                                </div>
                            ) : (
                                <p className="performance-empty">
                                    No account samples are available. Check
                                    Connections.
                                </p>
                            )}
                        </section>
                        <section
                            className="performance-section performance-account-section"
                            aria-labelledby="media-queue-heading"
                        >
                            <h3 id="media-queue-heading">
                                Waiting for Telegram
                            </h3>
                            <p className="settings-help">
                                Demand reads have priority over background
                                preparation. Estimates change with workload and
                                provider cooldowns.
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
                                            {telegram.queue.map(
                                                (entry, index) => (
                                                    <tr
                                                        key={
                                                            (entry.request_id ||
                                                                "queue") + index
                                                        }
                                                    >
                                                        <td>
                                                            {entry.position}
                                                        </td>
                                                        <td>
                                                            {entry.operation ===
                                                            "download"
                                                                ? "Download"
                                                                : "Upload"}
                                                        </td>
                                                        <td>
                                                            {entry.priority ===
                                                            "preload"
                                                                ? "Background"
                                                                : "Demand"}
                                                        </td>
                                                        <td>
                                                            {Math.floor(
                                                                entry.waiting_seconds,
                                                            )}
                                                            s
                                                        </td>
                                                        <td>
                                                            {queueWait(
                                                                entry.estimated_wait_seconds,
                                                            )}
                                                        </td>
                                                    </tr>
                                                ),
                                            )}
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
                            className="performance-section performance-account-section"
                            aria-labelledby="recent-load-heading"
                        >
                            <h3 id="recent-load-heading">
                                Recent Telegram load
                            </h3>
                            {history.length >= 2 && hasTelegramActivity ? (
                                <>
                                    <p className="settings-help">
                                        0–{ceiling} simultaneous operations ·{" "}
                                        {historyDuration(
                                            history.at(-1)!.at - history[0].at,
                                        )}{" "}
                                        of sampled activity
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
                                            points={loadPoints(
                                                history,
                                                "active",
                                                ceiling,
                                            )}
                                            className="performance-plot-active"
                                        />
                                        <polyline
                                            points={loadPoints(
                                                history,
                                                "queued",
                                                ceiling,
                                            )}
                                            className="performance-plot-queued"
                                        />
                                    </svg>
                                    <p className="settings-help">
                                        Recent observations from this running
                                        process.{" "}
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
                    </TabsPanel>
                    <TabsPanel value="server" keepMounted>
                        <PerformanceOverview
                            snapshot={snapshot}
                            current={current}
                            onAccounts={() => {
                                setView("telegram");
                                accountsTab.current?.focus();
                            }}
                            onResources={() => {
                                setView("storage");
                                resourcesTab.current?.focus();
                            }}
                        />
                        <section
                            className="performance-drive-summary"
                            aria-labelledby="drive-library-heading"
                        >
                            <div className="performance-drive-heading">
                                <h3 id="drive-library-heading">
                                    TGDrive library
                                </h3>
                                <a
                                    className="inline-link"
                                    href="/?path=%2Ftrash"
                                >
                                    Open Trash
                                </a>
                            </div>
                            {Number.isFinite(snapshot.drive?.files) &&
                            Number.isFinite(snapshot.drive?.folders) &&
                            Number.isFinite(snapshot.drive?.total_bytes) ? (
                                <dl>
                                    <div>
                                        <dt>Files</dt>
                                        <dd>
                                            {snapshot.drive.files.toLocaleString()}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Folders</dt>
                                        <dd>
                                            {snapshot.drive.folders.toLocaleString()}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Stored in Telegram</dt>
                                        <dd>
                                            {formatBytes(
                                                snapshot.drive.total_bytes,
                                            )}
                                        </dd>
                                    </div>
                                    {typeof snapshot.drive.trashed_files ===
                                        "number" &&
                                        Number.isFinite(
                                            snapshot.drive.trashed_files,
                                        ) && (
                                            <div>
                                                <dt>Files in Trash</dt>
                                                <dd>
                                                    {snapshot.drive.trashed_files.toLocaleString()}
                                                </dd>
                                                {typeof snapshot.drive
                                                    .trashed_bytes ===
                                                    "number" &&
                                                    Number.isFinite(
                                                        snapshot.drive
                                                            .trashed_bytes,
                                                    ) && (
                                                        <dd>
                                                            {formatBytes(
                                                                snapshot.drive
                                                                    .trashed_bytes,
                                                            )}
                                                        </dd>
                                                    )}
                                            </div>
                                        )}
                                </dl>
                            ) : (
                                <p className="settings-help">
                                    Library figures are unavailable.
                                </p>
                            )}
                        </section>
                        <div className="performance-history-panel">
                            <div className="performance-measurements-heading">
                                <div className="performance-history-title">
                                    <h3>Charts and history</h3>
                                    <p className="performance-history-availability">
                                        {historical
                                            ? recorded &&
                                              loadedRange === rangeKey
                                                ? `Recorded averages · ${historyDuration(recorded.bucket_seconds)} per interval. Network rate labels stay live. ${recorded.samples.some((sample) => sample.cpu_percent !== null || sample.memory_bytes !== null || sample.received_bytes_per_second !== null || sample.sent_bytes_per_second !== null) ? `History begins ${formatTimestamp(recorded.earliest_at)}.` : "No readings were recorded for this range."}`
                                                : range === "custom" &&
                                                    !customBounds
                                                  ? "Choose dates and apply a range to inspect recorded history."
                                                  : "Selected range has not loaded yet."
                                            : resourceHistory.length > 1
                                              ? `${historyDuration(resourceHistory.at(-1)!.at - resourceHistory[0].at)} of live readings`
                                              : "Collecting live history; earlier readings are not available yet."}
                                    </p>
                                </div>
                                <div className="performance-history-controls">
                                    <label>
                                        Time range
                                        <select
                                            aria-label="Time range"
                                            value={range}
                                            onChange={(event) => {
                                                const value = event.target
                                                    .value as HistoryRange;
                                                setRange(value);
                                                setHistoryError("");
                                                setCustomError("");
                                                try {
                                                    if (value !== "custom")
                                                        window.localStorage.setItem(
                                                            HISTORY_WINDOW_KEY,
                                                            value,
                                                        );
                                                } catch {
                                                    /* Keep working without storage. */
                                                }
                                            }}
                                        >
                                            <option value="live">
                                                Live (5 min)
                                            </option>
                                            <option value="hour">
                                                Last hour
                                            </option>
                                            <option value="day">
                                                Last 24 hours
                                            </option>
                                            <option value="week">
                                                Last 7 days
                                            </option>
                                            <option value="month">
                                                Last month (30 days)
                                            </option>
                                            <option value="custom">
                                                Custom range
                                            </option>
                                        </select>
                                    </label>
                                </div>
                            </div>
                            {range === "custom" && (
                                <form
                                    className="performance-custom-range"

                                    onSubmit={(event) => {
                                        event.preventDefault();
                                        try {
                                            setCustomBounds(
                                                customHistoryBounds(
                                                    customStart,
                                                    customEnd,
                                                ),
                                            );
                                            setHistoryError("");
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
                                            onChange={(event) =>
                                                setCustomStart(
                                                    event.target.value,
                                                )
                                            }
                                        />
                                    </label>
                                    <label>
                                        End (UTC)
                                        <input
                                            type="datetime-local"
                                            required
                                            value={customEnd}
                                            onChange={(event) =>
                                                setCustomEnd(event.target.value)
                                            }
                                        />
                                    </label>
                                    <button
                                        className="secondary-button"
                                        type="submit"
                                    >
                                        Apply Range
                                    </button>
                                    <p className="settings-help">
                                        Enter UTC times. Chart timestamps use{" "}
                                        {getTimezone()}. Up to 31 days; gaps
                                        mean no readings were recorded.
                                    </p>
                                    {customBounds && (
                                        <p className="settings-help">
                                            Applied:{" "}
                                            {formatTimestamp(
                                                customBounds.start,
                                            )}{" "}
                                            –{" "}
                                            {formatTimestamp(customBounds.end)}{" "}
                                            ({getTimezone()})
                                        </p>
                                    )}
                                    <ErrorNotice message={customError} />
                                </form>
                            )}
                            {historyLoading &&
                                historical &&
                                loadedRange !== rangeKey && (
                                    <p
                                        className="performance-history-status"
                                        role="status"
                                    >
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
                                                : "") +
                                            historyError +
                                            " Retrying automatically."
                                        }
                                    />
                                </div>
                            )}
                            <MetricChart
                                key={rangeKey}
                                historical={historical}
                                bucketSeconds={
                                    historical
                                        ? recorded?.bucket_seconds || 60
                                        : 3
                                }
                                title="Server network"
                                headingHelp={<NetworkMeasurementHelp />}
                                summary={
                                    <NetworkReadings
                                        snapshot={snapshot}
                                        current={current}
                                    />
                                }
                                samples={resourceHistory}
                                series={[
                                    {
                                        key: "sent_bytes_per_second",
                                        label: "Upload from server",
                                    },
                                    {
                                        key: "received_bytes_per_second",
                                        label: "Download to server",
                                        secondary: true,
                                    },
                                ]}
                                maximum={networkCeiling}
                                format={networkRate}
                                scope="network_interface"
                                current=""
                                note={
                                    "All traffic on the reported network interface; not Telegram alone" +
                                    (network?.interface
                                        ? " on " + network.interface
                                        : "") +
                                    "."
                                }
                            />
                            <div className="performance-resource-charts">
                                <MetricChart
                                    key={"cpu-" + rangeKey}
                                    historical={historical}
                                    bucketSeconds={
                                        historical
                                            ? recorded?.bucket_seconds || 60
                                            : 3
                                    }
                                    title={
                                        cpu?.scope === "host"
                                            ? "System CPU"
                                            : cpu?.scope === "cgroup"
                                              ? "Container CPU"
                                              : "CPU"
                                    }
                                    samples={resourceHistory}
                                    series={[
                                        { key: "cpu_percent", label: "Usage" },
                                    ]}
                                    maximum={100}
                                    format={(value) => value.toFixed(1) + "%"}
                                    scope="cpu_scope"
                                    current={
                                        typeof cpu?.percent === "number"
                                            ? cpu.percent.toFixed(1) + "%"
                                            : "Measuring…"
                                    }
                                    note={
                                        resourceSummary?.cpu.capacity_cores
                                            ? "Assigned CPU capacity (" +
                                              resourceSummary.cpu.scope +
                                              "): " +
                                              resourceSummary.cpu.capacity_cores.toLocaleString() +
                                              " CPU equivalents."
                                            : "Measured CPU utilization; missing readings remain gaps."
                                    }
                                />
                                <MetricChart
                                    key={"ram-" + rangeKey}
                                    historical={historical}
                                    bucketSeconds={
                                        historical
                                            ? recorded?.bucket_seconds || 60
                                            : 3
                                    }
                                    title={
                                        resourceSummary?.memory.scope === "host"
                                            ? "Visible system RAM"
                                            : resourceSummary?.memory.scope ===
                                                    "cgroup" ||
                                                memory?.cgroup_current != null
                                              ? "Container RAM"
                                              : "Server memory"
                                    }
                                    samples={resourceHistory}
                                    series={[
                                        {
                                            key: "memory_bytes",
                                            label: "In use",
                                        },
                                    ]}
                                    maximum={memoryCeiling}
                                    format={formatBytes}
                                    current={
                                        Number.isFinite(memoryUsed)
                                            ? formatBytes(memoryUsed) +
                                              (memoryLimit
                                                  ? " / " +
                                                    formatBytes(memoryLimit)
                                                  : " in use")
                                            : "Unavailable"
                                    }
                                    note={
                                        resourceSummary?.memory.scope === "host"
                                            ? "Visible operating system or VM memory total: " +
                                              formatBytes(memoryLimit) +
                                              ". This is not a container hard limit."
                                            : memoryLimit
                                              ? "Current container limit: " +
                                                formatBytes(memoryLimit) +
                                                ". Includes all processes in this container."
                                              : "No finite visible container limit established. Available system memory is shown separately; the graph scales to observed usage."
                                    }
                                />
                            </div>
                        </div>
                    </TabsPanel>
                    <TabsPanel
                        value="storage"
                        className="performance-storage-panel"
                        keepMounted
                    >
                        <section
                            className="performance-section"
                            aria-labelledby="server-resource-heading"
                        >
                            <div className="performance-section-title">
                                <h3 id="server-resource-heading">
                                    Disk and caches
                                </h3>
                            </div>
                            <div className="performance-resource-summary">
                                <div
                                    className="performance-usage"
                                    data-storage="effective"
                                >
                                    <div>
                                        <span>Verified free disk space</span>
                                        <strong>
                                            {backingFree === null
                                                ? "Unavailable"
                                                : formatBytes(backingFree)}
                                        </strong>
                                    </div>
                                    <small>
                                        {backingFree === null
                                            ? diskCapacity?.backing.configured
                                                ? "The configured disk did not return a reading."
                                                : "No host disk check configured."
                                            : (diskCapacity!.effective
                                                  .headroom_bytes === null
                                                  ? "Reserve headroom unavailable."
                                                  : formatBytes(
                                                        diskCapacity!.effective
                                                            .headroom_bytes,
                                                    ) +
                                                    " available after reserve.") +
                                              " Uses the filesystem and configured disk readings."}
                                    </small>
                                </div>
                                {snapshot.database && (
                                    <UsageMeter
                                        label="MongoDB storage"
                                        used={snapshot.database.used_bytes}
                                        total={
                                            snapshot.database
                                                .volume_total_bytes || NaN
                                        }
                                        unavailableLabel={formatBytes(
                                            snapshot.database.used_bytes,
                                        )}
                                        detail={
                                            formatBytes(
                                                snapshot.database.index_bytes,
                                            ) +
                                            " of it is search and listing indexes"
                                        }
                                    />
                                )}
                                <UsageMeter
                                    label="RAM cache"
                                    used={snapshot.cache.memory.bytes}
                                    total={
                                        snapshot.cache.memory
                                            .effective_max_bytes
                                    }
                                    detail={
                                        snapshot.cache.memory
                                            .effective_max_bytes === 0
                                            ? "Disabled"
                                            : "Retained media; part of TGDrive process memory"
                                    }
                                />
                                <UsageMeter
                                    label="Disk cache"
                                    used={snapshot.cache.disk.bytes}
                                    total={snapshot.cache.disk.max_bytes}
                                    detail={
                                        !snapshot.cache.disk.available
                                            ? "Unavailable"
                                            : snapshot.cache.disk.max_bytes ===
                                                0
                                              ? "Disabled"
                                              : "Disposable copies; originals stay in Telegram"
                                    }
                                />
                            </div>
                            {diskCapacity?.backing.configured && (
                                <div
                                    className="performance-capacity-readings"
                                    role="group"
                                    aria-label="Storage capacity measurements"
                                >
                                    <dl className="performance-facts">
                                        <div>
                                            <dt>Configured disk free</dt>
                                            <dd>
                                                {diskCapacity.backing
                                                    .available &&
                                                diskCapacity.backing
                                                    .free_bytes !== null
                                                    ? formatBytes(
                                                          diskCapacity.backing
                                                              .free_bytes,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                        </div>
                                        <div>
                                            <dt>Free after reserve</dt>
                                            <dd>
                                                {diskCapacity.effective
                                                    .available &&
                                                diskCapacity.effective
                                                    .headroom_bytes !== null
                                                    ? formatBytes(
                                                          diskCapacity.effective
                                                              .headroom_bytes,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                        </div>
                                    </dl>
                                    {!diskCapacity.effective.available && (
                                        <p
                                            className="settings-help"
                                            role="status"
                                        >
                                            New writes are unavailable because
                                            required storage capacity could not
                                            be measured.
                                        </p>
                                    )}
                                </div>
                            )}
                            <p className="performance-cache-link">
                                <a
                                    className="inline-link"
                                    href="/?view=preloads"
                                >
                                    View cached files
                                </a>
                                <span>
                                    {
                                        snapshot.cache.files.filter(
                                            cachedFileComplete,
                                        ).length
                                    }{" "}
                                    ready ·{" "}
                                    {
                                        snapshot.cache.files.filter(
                                            (file) => !cachedFileComplete(file),
                                        ).length
                                    }{" "}
                                    partial
                                </span>
                            </p>
                        </section>
                        <section
                            className="performance-section performance-staging"
                            aria-labelledby="staging-heading"
                        >
                            <div className="performance-section-title performance-staging-title">
                                <h3 id="staging-heading">
                                    Temporary upload storage
                                </h3>
                                <a
                                    className="inline-link"
                                    href="/?view=settings&section=transfers"
                                >
                                    Transfer limits
                                </a>
                            </div>
                            <p className="performance-sample-time">
                                {usageUpdatedAt === null
                                    ? usageError
                                        ? "Staging figures unavailable"
                                        : "Loading staging figures…"
                                    : "Upload space checked " +
                                      formatTimestamp(usageUpdatedAt)}
                            </p>
                            <ErrorNotice message={usageError} />
                            {usage && (
                                <div className="performance-staging-layout">
                                    <UsageMeter
                                        label="Upload staging reservations"
                                        used={usage.staging_reserved_bytes}
                                        total={usage.staging_budget_bytes}
                                        detail="Reserved by uploads in progress."
                                    />
                                    <dl className="performance-facts performance-memory-details">
                                        {stagingFree === null ? (
                                            <div>
                                                <dt>Upload disk space</dt>
                                                <dd>Unverified</dd>
                                                <dd>
                                                    {stagingCapacity?.backing
                                                        .configured
                                                        ? "The configured disk did not return a reading."
                                                        : "No host disk check configured."}
                                                </dd>
                                            </div>
                                        ) : (
                                            <>
                                                {" "}
                                                <div>
                                                    <dt>
                                                        Verified free disk space
                                                    </dt>
                                                    <dd>
                                                        {stagingFree === null
                                                            ? "Unavailable"
                                                            : formatBytes(
                                                                  stagingFree,
                                                              )}
                                                    </dd>
                                                    <dd>
                                                        Space before the
                                                        free-space reserve
                                                    </dd>
                                                </div>
                                                <div>
                                                    <dt>
                                                        Available after reserve
                                                    </dt>
                                                    <dd>
                                                        {stagingFree === null ||
                                                        stagingCapacity
                                                            ?.effective
                                                            .headroom_bytes ==
                                                            null
                                                            ? "Unavailable"
                                                            : formatBytes(
                                                                  stagingCapacity
                                                                      .effective
                                                                      .headroom_bytes,
                                                              )}
                                                    </dd>
                                                    <dd>
                                                        Outstanding upload
                                                        reservations are
                                                        separate
                                                    </dd>
                                                </div>
                                            </>
                                        )}
                                        <div>
                                            <dt>Maximum file size</dt>
                                            <dd>
                                                {typeof (
                                                    snapshot.tuning?.values
                                                        .max_file_size ??
                                                    maxFileSizeBytes
                                                ) === "number"
                                                    ? formatBytes(
                                                          (snapshot.tuning
                                                              ?.values
                                                              .max_file_size ??
                                                              maxFileSizeBytes)!,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                        </div>
                                    </dl>
                                </div>
                            )}
                        </section>
                        <section
                            className="performance-section"
                            aria-labelledby="resource-detail-heading"
                        >
                            <h3 id="resource-detail-heading">
                                Server diagnostics
                            </h3>
                            <div className="performance-resource-details">
                                <section
                                    aria-labelledby="memory-cache-heading"
                                    className="performance-detail-note"
                                >
                                    <h4 id="memory-cache-heading">
                                        Memory and cache details
                                    </h4>
                                    <dl className="performance-facts performance-memory-details">
                                        <div>
                                            <dt>TGDrive process</dt>
                                            <dd>
                                                {typeof snapshot.server
                                                    .process_rss_bytes ===
                                                "number"
                                                    ? formatBytes(
                                                          snapshot.server
                                                              .process_rss_bytes,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                            <dd>
                                                Part of server memory · peak{" "}
                                                {formatBytes(
                                                    snapshot.server
                                                        .process_peak_rss_bytes,
                                                )}
                                            </dd>
                                        </div>
                                        <div>
                                            <dt>
                                                Available to the operating
                                                system
                                            </dt>
                                            <dd>
                                                {typeof memory?.MemAvailable ===
                                                "number"
                                                    ? formatBytes(
                                                          memory.MemAvailable,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                            <dd>
                                                {typeof memory?.MemTotal ===
                                                "number"
                                                    ? "Visible system total " +
                                                      formatBytes(
                                                          memory.MemTotal,
                                                      )
                                                    : "Visible system memory is unavailable"}
                                            </dd>
                                        </div>
                                        <StreamMemoryFact
                                            scratch={snapshot.cache.scratch}
                                        />
                                        <div>
                                            <dt>RAM cache lookups</dt>
                                            <dd>
                                                {snapshot.cache.memory.hits.toLocaleString()}{" "}
                                                hits ·{" "}
                                                {snapshot.cache.memory.misses.toLocaleString()}{" "}
                                                misses
                                            </dd>
                                            <dd>
                                                Block lookups, including
                                                preloading; not a
                                                bandwidth-saving percentage
                                            </dd>
                                        </div>
                                    </dl>
                                </section>
                                <section
                                    className="performance-detail-note"
                                    aria-labelledby="measurement-scope-heading"
                                >
                                    <h4 id="measurement-scope-heading">
                                        Resource measurements
                                        <SettingHelp label="Resource measurements">
                                            <p>
                                                Measurements describe this
                                                server's visible container,
                                                operating system or VM. They do
                                                not establish physical-host
                                                capacity. Free after reserve is
                                                separate from outstanding upload
                                                reservations.
                                            </p>
                                        </SettingHelp>
                                    </h4>
                                    <dl className="performance-facts performance-memory-details">
                                        <div>
                                            <dt>Assigned CPU capacity</dt>
                                            <dd>
                                                {resourceSummary?.cpu
                                                    .capacity_cores != null
                                                    ? resourceSummary.cpu.capacity_cores.toLocaleString() +
                                                      " CPU equivalents"
                                                    : "Unavailable"}
                                            </dd>
                                            <dd>
                                                {resourceSummary?.cpu.scope ===
                                                "cgroup"
                                                    ? "Container CPU allocation"
                                                    : resourceSummary?.cpu
                                                            .scope ===
                                                        "affinity"
                                                      ? "CPUs available to this process"
                                                      : resourceSummary?.cpu
                                                              .scope === "host"
                                                        ? "CPU visible to the operating system"
                                                        : "CPU scope unavailable"}
                                            </dd>
                                        </div>
                                        <div>
                                            <dt>Available to TGDrive</dt>
                                            <dd>
                                                {resourceSummary?.memory
                                                    .available_bytes != null
                                                    ? formatBytes(
                                                          resourceSummary.memory
                                                              .available_bytes,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                            <dd>
                                                {resourceSummary?.memory
                                                    .scope === "cgroup"
                                                    ? "Container and visible system headroom"
                                                    : resourceSummary?.memory
                                                            .scope === "host"
                                                      ? "Operating system or VM headroom"
                                                      : "Scope unavailable"}
                                            </dd>
                                        </div>
                                        <div>
                                            <dt>
                                                Memory reference for automatic
                                                limits
                                            </dt>
                                            <dd>
                                                {resourceSummary?.memory
                                                    .limit_bytes != null
                                                    ? formatBytes(
                                                          resourceSummary.memory
                                                              .limit_bytes,
                                                      )
                                                    : "Unavailable"}
                                            </dd>
                                            <dd>
                                                {resourceSummary?.memory
                                                    .limit_source ===
                                                "proc_meminfo"
                                                    ? "Uses the memory reported by Linux to size caches and stream buffers."
                                                    : resourceSummary?.memory
                                                            .limit_source ===
                                                            "cgroup_v1" ||
                                                        resourceSummary?.memory
                                                            .limit_source ===
                                                            "cgroup_v2"
                                                      ? "Uses the container memory limit to size caches and stream buffers."
                                                      : "Used to size caches and stream buffers."}
                                            </dd>
                                        </div>
                                        <div>
                                            <dt>Host disk check</dt>
                                            <dd>
                                                {resourceSummary?.storage.data
                                                    .backing.configured
                                                    ? resourceSummary.storage
                                                          .data.backing
                                                          .available
                                                        ? "Available"
                                                        : "Unavailable"
                                                    : "Not configured"}
                                            </dd>
                                        </div>
                                    </dl>
                                </section>
                            </div>
                            <section
                                aria-labelledby="filesystem-measurement-heading"
                                className="performance-detail-note"
                            >
                                <h4 id="filesystem-measurement-heading">
                                    Filesystem measurement details
                                </h4>
                                <dl className="performance-facts performance-memory-details">
                                    <div>
                                        <dt>Total space</dt>
                                        <dd>
                                            {snapshot.server.disk.available ===
                                            false
                                                ? "Unavailable"
                                                : formatBytes(
                                                      snapshot.server.disk
                                                          .total_bytes,
                                                  )}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Used space</dt>
                                        <dd>
                                            {snapshot.server.disk.available ===
                                            false
                                                ? "Unavailable"
                                                : formatBytes(
                                                      snapshot.server.disk
                                                          .used_bytes,
                                                  )}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Free space</dt>
                                        <dd>
                                            {snapshot.server.disk.available ===
                                            false
                                                ? "Unavailable"
                                                : formatBytes(
                                                      snapshot.server.disk
                                                          .free_bytes,
                                                  )}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>Free on the upload filesystem</dt>
                                        <dd>
                                            {usage?.free_disk_bytes == null
                                                ? "Unavailable"
                                                : formatBytes(
                                                      usage.free_disk_bytes,
                                                  )}
                                        </dd>
                                    </div>
                                </dl>
                                <p className="settings-help">
                                    These are the filesystem measurements
                                    visible to TGDrive. A container or VM may
                                    report a different capacity from its host
                                    disk.
                                </p>
                            </section>
                        </section>
                    </TabsPanel>
                    <TabsPanel value="tuning" keepMounted>
                        <nav
                            className="performance-tuning-links"
                            aria-label="Performance settings"
                        >
                            <a
                                className="inline-link"
                                href="/?view=settings&section=cache"
                            >
                                Cache settings
                            </a>
                            <a
                                className="inline-link"
                                href="/?view=settings&section=transfers"
                            >
                                Upload and download speed limits
                            </a>
                        </nav>
                        <AutomaticTuning
                            tuning={snapshot.tuning}
                            current={current}
                            serverAt={snapshot.server.at}
                        />
                        {recorded && (
                            <section
                                className="performance-all-time"
                                aria-labelledby="all-time-heading"
                            >
                                <h3 id="all-time-heading">All-time totals</h3>
                                <p className="settings-help">
                                    {recorded.tracked_since
                                        ? `Since ${formatTimestamp(recorded.tracked_since)}.`
                                        : "Tracking has just started."}{" "}
                                    Totals survive restart and are independent
                                    of the time range.
                                </p>
                                <table className="performance-totals-table">
                                    <thead>
                                        <tr>
                                            <th scope="col">Traffic source</th>
                                            <th scope="col">
                                                Received by server
                                            </th>
                                            <th scope="col">Sent by server</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {(
                                            [
                                                [
                                                    "Server network",
                                                    recorded.totals
                                                        .network_received_bytes,
                                                    recorded.totals
                                                        .network_sent_bytes,
                                                ],
                                                [
                                                    "TGDrive files",
                                                    recorded.totals
                                                        .http_upload_bytes,
                                                    recorded.totals
                                                        .http_download_bytes,
                                                ],
                                                [
                                                    "Telegram",
                                                    recorded.totals
                                                        .telegram_download_bytes,
                                                    recorded.totals
                                                        .telegram_upload_bytes,
                                                ],
                                            ] as const
                                        ).map(([label, received, sent]) => (
                                            <tr key={label}>
                                                <th scope="row">{label}</th>
                                                <td>
                                                    {typeof received ===
                                                    "number"
                                                        ? formatBytes(received)
                                                        : "Not available"}
                                                </td>
                                                <td>
                                                    {typeof sent === "number"
                                                        ? formatBytes(sent)
                                                        : "Not available"}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                                <p className="settings-help">
                                    Network includes Telegram, URL imports and
                                    device traffic. The rows overlap; do not add
                                    them together. Totals start when tracking
                                    begins.
                                </p>
                            </section>
                        )}
                    </TabsPanel>
                </>
            )}
        </Tabs>
    );
}
