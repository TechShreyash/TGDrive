import { Tabs, Paper } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { formatTimestamp, getTimezone } from "../../shared/types";
import StreamMemoryFact from "./StreamMemoryFact";
import PerformanceExplanation from "./PerformanceExplanation";
import { cachedFileComplete } from "./Preloads";
import { useManualRefresh } from "../../shared/useManualRefresh";
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
        title: "Transfer limits",
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
    cache_memory_bytes: "Fast RAM for active media streams and thumbnails",
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
}: {
    tuning: NonNullable<PerformanceSnapshot["tuning"]>;
}) {
    const categorized = new Set<string>(
        TUNING_CATEGORIES.flatMap((c) => c.keys),
    );
    const otherKeys = Object.keys(tuning.values).filter(
        (key) => !categorized.has(key),
    );
    const groups = [
        ...TUNING_CATEGORIES.map((cat) => ({
            title: cat.title,
            keys: cat.keys.filter((k) => k in tuning.values),
        })).filter((group) => group.keys.length > 0),
        ...(otherKeys.length > 0
            ? [
                  {
                      title: "Other Parameters",
                      keys: otherKeys,
                  },
              ]
            : []),
    ];

    return (
        <section
            className="performance-section tuning-section"
            aria-labelledby="automatic-tuning-heading"
        >
            <div className="performance-section-title">
                <h3 id="automatic-tuning-heading">Automatic tuning</h3>
                <span className="performance-summary-value">
                    {tuning.running ? "Resource sizing" : "Not running"}
                </span>
            </div>
            <details className="performance-detail-note">
                <summary>Applied limits and reasons</summary>
                <p className="tuning-intro">
                    {tuning.running
                        ? "Cache and staging follow available resources. Transfer limits remain configured; preloads also have a CPU cap. Telegram cooldowns apply per account."
                        : "Automatic tuning starts with the drive."}
                </p>
                <div className="tuning-groups">
                    {groups.map((group) => (
                        <div key={group.title} className="tuning-group">
                            <div className="tuning-group-header">
                                <h4>{group.title}</h4>
                            </div>
                            <dl className="performance-facts tuning-facts">
                                {group.keys.map((key) => {
                                    const label = TUNING_LABELS[key] || key;
                                    const val =
                                        key.endsWith("_bytes") ||
                                        key === "max_file_size"
                                            ? formatBytes(tuning.values[key])
                                            : tuning.values[
                                                  key
                                              ].toLocaleString();
                                    const reason = tuning.reasons[key];
                                    const fallback = TUNING_DESCRIPTIONS[key];
                                    return (
                                        <div
                                            key={key}
                                            className="tuning-fact-item"
                                        >
                                            <div className="tuning-fact-top">
                                                <dt>{label}</dt>
                                                <dd>{val}</dd>
                                            </div>
                                            {(reason || fallback) && (
                                                <small className="tuning-fact-detail">
                                                    {reason || fallback}
                                                </small>
                                            )}
                                        </div>
                                    );
                                })}
                            </dl>
                        </div>
                    ))}
                </div>
                <p className="settings-help">
                    These are effective budgets, not reserved physical capacity.
                    Set owner ceilings in Cache settings and Transfer limits.
                </p>
            </details>
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
    hint?: string;
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
    const [hoverPosition, setHoverPosition] = useState({ left: 8, top: 8 });
    const tooltip = useRef<HTMLDivElement>(null);
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
                <h3>{title}</h3>
                {!historical && (
                    <strong title="Current reading">
                        {current}
                        <small className="performance-chart-now">Now</small>
                    </strong>
                )}
            </div>
            {series.length > 1 && (
                <div className="performance-chart-legend">
                    {series.map(({ key, label, secondary }) => (
                        <span
                            key={key}
                            className={
                                secondary
                                    ? "chart-series-secondary"
                                    : "chart-series-primary"
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
                            onPointerMove={(event) => {
                                if (event.pointerType !== "mouse") return;
                                const bounds =
                                    event.currentTarget.getBoundingClientRect();
                                const position = Math.max(
                                    0,
                                    Math.min(
                                        1,
                                        (event.clientX - bounds.left) /
                                            bounds.width,
                                    ),
                                );
                                const target =
                                    first + position * (last - first);
                                const nearest = nearestSampleAt(
                                    samples,
                                    target,
                                );
                                const plot =
                                    event.currentTarget.parentElement!.getBoundingClientRect();
                                const width =
                                    tooltip.current?.offsetWidth ??
                                    Math.min(280, plot.width - 16);
                                const height =
                                    tooltip.current?.offsetHeight ?? 90;
                                const pointerX = event.clientX - plot.left;
                                const pointerY = event.clientY - plot.top;
                                setHoverPosition({
                                    left: Math.max(
                                        8,
                                        Math.min(
                                            plot.width - width - 8,
                                            pointerX + width + 16 < plot.width
                                                ? pointerX + 14
                                                : pointerX - width - 14,
                                        ),
                                    ),
                                    top: Math.max(
                                        8,
                                        Math.min(
                                            plot.height - height - 8,
                                            pointerY + 16,
                                        ),
                                    ),
                                });
                                setHoverAt(nearest);
                            }}
                            onPointerLeave={() => setHoverAt(null)}
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
                            {series.flatMap(({ key, secondary }) =>
                                samples.flatMap((sample, sampleIndex) => {
                                    const value = sample[key];
                                    return value === null ||
                                        !Number.isFinite(value) ||
                                        value < 0
                                        ? []
                                        : [
                                              <circle
                                                  key={key + ":" + sampleIndex}
                                                  cx={
                                                      (last === first
                                                          ? 1
                                                          : (sample.at -
                                                                first) /
                                                            (last - first)) *
                                                      600
                                                  }
                                                  cy={
                                                      112 -
                                                      Math.min(
                                                          1,
                                                          value /
                                                              Math.max(
                                                                  1,
                                                                  maximum,
                                                              ),
                                                      ) *
                                                          100
                                                  }
                                                  r={2}
                                                  className={
                                                      secondary
                                                          ? "performance-chart-point-secondary"
                                                          : "performance-chart-point"
                                                  }
                                              />,
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
                            <div
                                ref={tooltip}
                                className="performance-chart-tooltip"
                                aria-hidden="true"
                                style={hoverPosition}
                            >
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
                        <span>
                            {historical ? metricTime(last) : "Latest reading"}
                        </span>
                    </div>
                    <label className="performance-chart-scrubber">
                        <span className="sr-only">
                            {"Inspect " + title + " samples"}
                        </span>
                        <input
                            type="range"
                            disabled={samples.length < 2}
                            min={0}
                            max={samples.length - 1}
                            value={Math.max(0, index)}
                            onChange={(event) => {
                                setHoverAt(null);
                                setSelectedAt(
                                    samples[Number(event.target.value)].at,
                                );
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
                                    secondary
                                        ? "chart-series-secondary"
                                        : "chart-series-primary"
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
            {quiet && (
                <p className="performance-chart-idle">
                    Idle during this observed window.
                </p>
            )}
            <details className="performance-detail-note performance-chart-details">
                <summary>Measurement scope</summary>
                <p className="performance-chart-note">{note}</p>
                {series.some((item) => item.hint) && (
                    <dl className="performance-facts">
                        {series
                            .filter((item) => item.hint)
                            .map(({ key, label, hint }) => (
                                <div key={key}>
                                    <dt>{label}</dt>
                                    <dd>{hint}</dd>
                                </div>
                            ))}
                    </dl>
                )}
            </details>
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
export default function PerformancePanel({
    onNotice,
}: {
    onNotice?: (message: string, error?: boolean) => void;
} = {}) {
    const [snapshot, setSnapshot] = useState<PerformanceSnapshot | null>(null);
    const [error, setError] = useState("");
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
    const [view, setView] = useState<"overview" | "telegram" | "server">(
        "overview",
    );
    const latestSnapshot = useRef(0);
    const latestHistory = useRef(0);
    const manualLoading = useRef(false);
    const { refreshing, run } = useManualRefresh(onNotice);
    const loadSnapshot = useCallback(
        async (signal: AbortSignal, manual = false) => {
            if (!manual && manualLoading.current) return false;
            const request = ++latestSnapshot.current;
            try {
                const value = await performanceApi.snapshot(signal);
                if (signal.aborted || request !== latestSnapshot.current)
                    return false;
                setSnapshot(value);
                setError("");
                return true;
            } catch (cause) {
                if (!signal.aborted && request === latestSnapshot.current)
                    setError(errorMessage(cause));
                if (manual) throw cause;
                return false;
            } finally {
                if (!signal.aborted && request === latestSnapshot.current)
                    setLoading(false);
            }
        },
        [],
    );
    const loadHistory = useCallback(
        async (signal: AbortSignal, manual = false) => {
            if (!manual && manualLoading.current) return false;
            const request = ++latestHistory.current;
            if (range === "custom" && !customBounds) {
                setHistoryError("");
                setHistoryLoading(false);
                return true;
            }
            setHistoryLoading(true);
            if (!manual) setHistoryError("");
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
                if (signal.aborted || request !== latestHistory.current)
                    return false;
                setRecorded(data);
                setLoadedRange(rangeKey);
                setHistoryError("");
                return true;
            } catch (cause) {
                if (!signal.aborted && request === latestHistory.current)
                    setHistoryError(errorMessage(cause));
                if (manual) throw cause;
                return false;
            } finally {
                if (!signal.aborted && request === latestHistory.current)
                    setHistoryLoading(false);
            }
        },
        [range, customBounds, rangeKey],
    );
    const refreshPerformance = () =>
        void run(async (signal) => {
            manualLoading.current = true;
            try {
                const results = await Promise.allSettled([
                    loadSnapshot(signal, true),
                    loadHistory(signal, true),
                ]);
                for (const result of results)
                    if (result.status === "rejected") throw result.reason;
                return results.every(
                    (result) => result.status === "fulfilled" && result.value,
                );
            } finally {
                manualLoading.current = false;
            }
        }, "Performance refreshed.");
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            await loadSnapshot(controller.signal);
            if (!controller.signal.aborted)
                timer = setTimeout(poll, document.hidden ? 15000 : 3000);
        };
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [loadSnapshot]);
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            await loadHistory(controller.signal);
            if (!controller.signal.aborted && range !== "custom")
                timer = setTimeout(poll, 60000);
        };
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [range, loadHistory]);
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
    const diskCapacity = snapshot?.server.disk.capacity;
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
    const networkRate = (value: number | null | undefined) =>
        typeof value === "number" ? formatBytes(value) + "/s" : "Measuring…";
    return (
        <Tabs
            className="performance-page page-body"
            id="performance"
            value={view}
            onChange={(value) => {
                if (
                    value === "overview" ||
                    value === "telegram" ||
                    value === "server"
                )
                    setView(value);
            }}
        >
            <div
                className="performance-section-heading performance-toolbar"
                data-view={view}
            >
                <Tabs.List
                    className="performance-view-tabs"
                    aria-label="Performance views"
                >
                    <Tabs.Tab value="overview">Overview</Tabs.Tab>
                    <Tabs.Tab value="telegram">Accounts</Tabs.Tab>
                    <Tabs.Tab value="server">Resources</Tabs.Tab>
                </Tabs.List>
                <div className="performance-history-controls">
                    <button
                        className="secondary-button"
                        disabled={refreshing}
                        aria-busy={refreshing}
                        onClick={refreshPerformance}
                        aria-label="Refresh Performance"
                    >
                        {refreshing ? <Spinner /> : <RefreshCw size={16} />}
                        <span>{refreshing ? "Refreshing…" : "Refresh"}</span>
                    </button>
                </div>
            </div>
            {loading && (
                <p className="performance-loading" role="status">
                    <Spinner /> Loading server performance…
                </p>
            )}
            <ErrorNotice message={error} />
            {snapshot && telegram && (
                <>
                    <Tabs.Panel
                        value="overview"
                        className="performance-overview-grid"
                    >
                        <PerformanceExplanation
                            diagnosis={snapshot.diagnosis}
                            serverAt={snapshot.traffic?.at}
                            liveRates={{
                                toViewers: sendRate ?? null,
                                fromDevices: receiveRate ?? null,
                            }}
                            telegram={{
                                active: telegram.active,
                                queued: telegram.queued,
                            }}
                            onAccounts={() => setView("telegram")}
                        />
                        <section
                            className="performance-section performance-overview-resources"
                            aria-labelledby="overview-resources-heading"
                        >
                            <div className="performance-section-title">
                                <h3 id="overview-resources-heading">
                                    Server resources
                                </h3>
                                <button
                                    type="button"
                                    className="inline-link"
                                    onClick={() => setView("server")}
                                >
                                    View resource history
                                </button>
                            </div>
                            <div className="performance-overview-meters">
                                <UsageMeter
                                    label="CPU usage"
                                    used={cpu?.percent ?? NaN}
                                    total={100}
                                    percent
                                    detail={
                                        cpu?.scope === "cgroup"
                                            ? "Container CPU"
                                            : cpu?.scope === "host"
                                              ? "System CPU"
                                              : "Scope unavailable"
                                    }
                                />
                                <UsageMeter
                                    label="Container RAM"
                                    used={memoryUsed}
                                    total={memoryLimit || NaN}
                                    unavailableLabel={
                                        Number.isFinite(memoryUsed)
                                            ? formatBytes(memoryUsed) +
                                              " in use"
                                            : "Unavailable"
                                    }
                                    detail={
                                        memoryLimit > 0
                                            ? "All processes in this container"
                                            : "No container limit reported"
                                    }
                                />
                                <UsageMeter
                                    label="Disk cache"
                                    used={
                                        snapshot.cache.disk.available
                                            ? snapshot.cache.disk.bytes
                                            : NaN
                                    }
                                    total={snapshot.cache.disk.max_bytes}
                                    detail="Retained media copies"
                                />
                                <UsageMeter
                                    label="RAM cache"
                                    used={snapshot.cache.memory.bytes}
                                    total={
                                        snapshot.cache.memory
                                            .effective_max_bytes
                                    }
                                    detail="Retained media in memory"
                                />
                            </div>
                            <p className="performance-overview-resource-note">
                                <a
                                    className="inline-link"
                                    href="/?view=settings&section=cache"
                                >
                                    Cache limits
                                </a>
                                <span>
                                    Cache copies are disposable; originals stay
                                    in Telegram.
                                </span>
                            </p>
                        </section>
                    </Tabs.Panel>
                    <Tabs.Panel
                        value="telegram"
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
                            <p className="settings-help">
                                Live account activity and current limits.
                            </p>
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
                                            {telegram.accounts.map(
                                                (account) => (
                                                    <tr key={account.name}>
                                                        <th scope="row">
                                                            {account.name}
                                                            <small>
                                                                {account.kind ===
                                                                "user"
                                                                    ? "Premium account"
                                                                    : "Storage bot"}
                                                            </small>
                                                        </th>
                                                        <td>
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
                                                        <td>
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
                                                        <td>
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
                                                        <td>
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
                                                        <td>
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
                                                                : "Ready"}
                                                            <small>
                                                                {account.requests
                                                                    ? account
                                                                          .requests
                                                                          .floods +
                                                                      " waits observed"
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
                    </Tabs.Panel>
                    <Tabs.Panel value="server">
                        <div className="performance-measurements-heading">
                            <h3>Charts and history</h3>
                            <div className="performance-history-controls">
                                {" "}
                                <label>
                                    Time range
                                    <select
                                        aria-label="Time range"
                                        disabled={refreshing}
                                        value={range}
                                        onChange={(event) => {
                                            const value = event.target
                                                .value as HistoryRange;
                                            setRange(value);
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
                                        <option value="hour">Last hour</option>
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
                                            setCustomStart(event.target.value)
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
                                    disabled={refreshing}
                                >
                                    Apply Range
                                </button>
                                <p className="settings-help">
                                    Enter UTC times. Chart timestamps use{" "}
                                    {getTimezone()}. Up to 31 days; gaps mean no
                                    readings were recorded.
                                </p>
                                {customBounds && (
                                    <p className="settings-help">
                                        Applied:{" "}
                                        {formatTimestamp(customBounds.start)} –{" "}
                                        {formatTimestamp(customBounds.end)} (
                                        {getTimezone()})
                                    </p>
                                )}
                                <ErrorNotice message={customError} />
                            </form>
                        )}
                        {historyLoading && historical && (
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
                                            : "") + historyError
                                    }
                                />
                                <button
                                    type="button"
                                    className="text-button"
                                    disabled={refreshing}
                                    aria-busy={refreshing}
                                    onClick={refreshPerformance}
                                >
                                    {refreshing && <Spinner />}
                                    {refreshing
                                        ? "Refreshing…"
                                        : "Retry History"}
                                </button>
                            </div>
                        )}
                        <p className="performance-history-availability">
                            {historical
                                ? recorded && loadedRange === rangeKey
                                    ? `Recorded interval averages (${historyDuration(recorded.bucket_seconds)} per point). Current resource usage below stays live; chart points belong to the selected range. ${recorded.samples.some((sample) => sample.cpu_percent !== null || sample.memory_bytes !== null || sample.received_bytes_per_second !== null || sample.sent_bytes_per_second !== null) ? `Available history begins ${formatTimestamp(recorded.earliest_at)}.` : "No readings were recorded for this range."}`
                                    : range === "custom" && !customBounds
                                      ? "Choose dates and apply a range to inspect recorded history."
                                      : "Selected range has not loaded yet."
                                : resourceHistory.length > 1
                                  ? `Showing ${historyDuration(resourceHistory.at(-1)!.at - resourceHistory[0].at)} of collected live history; up to 5 min selected.`
                                  : "Collecting live history; earlier readings are not available yet."}
                        </p>
                        <MetricChart
                            windowSeconds={windowSeconds}
                            historical={historical}
                            bucketSeconds={
                                historical ? recorded?.bucket_seconds || 60 : 3
                            }
                            title="Server network"
                            samples={resourceHistory}
                            series={[
                                {
                                    key: "received_bytes_per_second",
                                    label: "Received by server",
                                    hint: "all traffic on the reported interface",
                                },
                                {
                                    key: "sent_bytes_per_second",
                                    label: "Sent by server",
                                    hint: "all traffic on the reported interface",
                                    secondary: true,
                                },
                            ]}
                            maximum={networkCeiling}
                            format={(value) => formatBytes(value) + "/s"}
                            scope="network_interface"
                            current={
                                "Received " +
                                networkRate(
                                    network?.received_bytes_per_second,
                                ) +
                                " / Sent " +
                                networkRate(network?.sent_bytes_per_second)
                            }
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
                                windowSeconds={windowSeconds}
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
                                    cpu?.capacity_cores
                                        ? "Reported CPU capacity: " +
                                          cpu.capacity_cores.toLocaleString() +
                                          " CPU equivalents."
                                        : "Measured CPU utilization; missing readings remain gaps."
                                }
                            />
                            <MetricChart
                                windowSeconds={windowSeconds}
                                historical={historical}
                                bucketSeconds={
                                    historical
                                        ? recorded?.bucket_seconds || 60
                                        : 3
                                }
                                title="Container RAM"
                                samples={resourceHistory}
                                series={[
                                    { key: "memory_bytes", label: "In use" },
                                ]}
                                maximum={memoryCeiling}
                                format={formatBytes}
                                current={
                                    Number.isFinite(memoryUsed)
                                        ? formatBytes(memoryUsed) +
                                          (memoryLimit
                                              ? " / " + formatBytes(memoryLimit)
                                              : " in use · limit unavailable")
                                        : "Unavailable"
                                }
                                note={
                                    memoryLimit
                                        ? "Current container limit: " +
                                          formatBytes(memoryLimit) +
                                          ". Includes all processes in this container."
                                        : "No container memory limit reported. The graph scales to observed usage, not total capacity."
                                }
                            />
                        </div>
                        <section
                            className="performance-section"
                            aria-labelledby="server-resource-heading"
                        >
                            <div className="performance-section-title">
                                <h3 id="server-resource-heading">
                                    Storage &amp; cache
                                </h3>
                                <span className="performance-chart-now">
                                    Now
                                </span>
                            </div>
                            <div className="performance-resource-summary">
                                <UsageMeter
                                    label="Data filesystem"
                                    used={
                                        snapshot.server.disk.available === false
                                            ? NaN
                                            : snapshot.server.disk.used_bytes
                                    }
                                    total={snapshot.server.disk.total_bytes}
                                    detail={
                                        snapshot.server.disk.available === false
                                            ? "Filesystem measurement unavailable"
                                            : formatBytes(
                                                  snapshot.server.disk
                                                      .free_bytes,
                                              ) +
                                              " reported free on this filesystem"
                                    }
                                />
                                {snapshot.database && (
                                    <UsageMeter
                                        label="MongoDB"
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
                                            : "Retained media; part of container memory"
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
                                            <dt>Backing filesystem free</dt>
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
                            <details className="performance-detail-note">
                                <summary>Memory and cache details</summary>
                                <dl className="performance-facts performance-memory-details">
                                    <div>
                                        <dt>TGDrive process</dt>
                                        <dd>
                                            {typeof snapshot.server
                                                .process_rss_bytes === "number"
                                                ? formatBytes(
                                                      snapshot.server
                                                          .process_rss_bytes,
                                                  )
                                                : "Unavailable"}
                                        </dd>
                                        <small>
                                            Part of container RAM · peak{" "}
                                            {formatBytes(
                                                snapshot.server
                                                    .process_peak_rss_bytes,
                                            )}
                                        </small>
                                    </div>
                                    <div>
                                        <dt>Host memory available</dt>
                                        <dd>
                                            {typeof memory?.MemAvailable ===
                                            "number"
                                                ? formatBytes(
                                                      memory.MemAvailable,
                                                  )
                                                : "Unavailable"}
                                        </dd>
                                        <small>
                                            {typeof memory?.MemTotal ===
                                            "number"
                                                ? "Host total " +
                                                  formatBytes(memory.MemTotal)
                                                : "Host memory is unavailable"}
                                        </small>
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
                                        <small>
                                            Block lookups, including preloading;
                                            not a bandwidth-saving percentage
                                        </small>
                                    </div>
                                </dl>
                                <p className="settings-help">
                                    Upload staging budget:{" "}
                                    {formatBytes(
                                        snapshot.server.staging_budget_bytes,
                                    )}
                                    .{" "}
                                    <a
                                        className="inline-link"
                                        href="/?view=storage"
                                    >
                                        Storage &amp; Backups
                                    </a>
                                </p>
                            </details>
                            <details className="performance-detail-note">
                                <summary>Resource measurement details</summary>
                                <p className="settings-help">
                                    Data filesystem readings describe the
                                    filesystem visible to TGDrive. Free after
                                    reserve uses the smaller measured free space
                                    and subtracts the configured reserve; it
                                    does not subtract outstanding upload
                                    reservations.
                                    {!diskCapacity?.backing.configured &&
                                        " Backing storage is not separately measured; a virtual filesystem can report more free space than its host can provide."}
                                </p>
                                <p className="settings-help">
                                    {network?.scope ||
                                        "Network interface scope unavailable."}{" "}
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
                        {snapshot.tuning && (
                            <AutomaticTuning tuning={snapshot.tuning} />
                        )}
                        {recorded && (
                            <details
                                className="performance-all-time performance-detail-note"
                                aria-labelledby="all-time-heading"
                            >
                                <summary>
                                    <h3 id="all-time-heading">
                                        All-time totals
                                    </h3>
                                </summary>
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
                                    them together. Usage before tracking began
                                    is not included.
                                </p>
                            </details>
                        )}
                    </Tabs.Panel>
                </>
            )}
        </Tabs>
    );
}
