import { useEffect, useState } from "react";
import { formatBytes, formatTimestamp } from "../../shared/types";
import type { PerformanceDiagnosis } from "./performanceApi";

const WAITS: Record<
    string,
    { title: string; action: string; metric: string; metricLabel: string }
> = {
    telegram_queue: {
        title: "Telegram queue",
        metric: "pool_queued_operations",
        metricLabel: "Pool queue",
        action: "Open Accounts to inspect queues and configured limits.",
    },
    telegram_cooldown: {
        title: "Telegram cooldown",
        metric: "longest_cooldown_seconds",
        metricLabel: "Longest cooldown",
        action: "Wait for the reported cooldown, then recheck Accounts.",
    },
    scratch_wait: {
        title: "Stream-buffer wait",
        metric: "waiting_heads",
        metricLabel: "Streams waiting",
        action: "Let current reads finish or reduce simultaneous reads.",
    },
};
const EVIDENCE: Record<string, string> = {
    pool_queued_operations: "Queued pool operations",
    observed_account_queued_requests:
        "Queued requests across reporting accounts",
    configured_operations_per_account:
        "Largest configured operation ceiling among reporting accounts",
    cooling_accounts_during_queue: "Cooling accounts while work is queued",
    longest_cooldown_seconds: "Longest reported cooldown",
    waiting_heads: "Streams waiting for buffer admission",
    reserved_bytes: "Reserved stream-buffer memory",
    effective_limit_bytes: "Effective stream-buffer memory limit",
};
const WORKLOAD = {
    playback_requests: "Playback",
    download_requests: "Downloads",
    unknown_file_requests: "Other files",
    upload_requests: "Device uploads",
};
function measured(value: number | null | undefined, unit = "") {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
        return "Unavailable";
    if (unit === "bytes") return formatBytes(value);
    if (unit === "bytes/s") return formatBytes(value) + "/s";
    return (
        value.toLocaleString(undefined, { maximumFractionDigits: 1 }) +
        (unit ? " " + unit : "")
    );
}

export default function PerformanceExplanation({
    diagnosis,
    serverAt,
}: {
    diagnosis?: PerformanceDiagnosis | null;
    serverAt?: number;
} = {}) {
    const [expired, setExpired] = useState<PerformanceDiagnosis | null>(null);
    // Compare clocks only on the server; expire retained responses locally.
    const age =
        diagnosis?.sampled_at != null && Number.isFinite(serverAt)
            ? serverAt! - diagnosis.sampled_at
            : NaN;
    const validAge =
        Number.isFinite(age) &&
        age >= 0 &&
        Number.isFinite(diagnosis?.stale_after_seconds) &&
        age <= diagnosis!.stale_after_seconds;
    const available =
        validAge && diagnosis?.state !== "unavailable" && expired !== diagnosis;
    useEffect(() => {
        if (!diagnosis || !validAge || diagnosis.state === "unavailable")
            return;
        const timer = setTimeout(
            () => setExpired(diagnosis),
            Math.max(0, (diagnosis.stale_after_seconds - age) * 1000),
        );
        return () => clearTimeout(timer);
    }, [diagnosis, age, validAge]);
    const summary = !diagnosis
        ? "Performance explanations are unavailable. Raw measurements remain available below."
        : !available
          ? "Evidence is unavailable or stale. Refresh Performance to update."
          : diagnosis.state === "idle"
            ? "No workload activity was observed in this window."
            : diagnosis.state === "observing"
              ? "Collecting samples; early readings are inconclusive."
              : diagnosis.constraints.length
                ? "Reported waits in the latest sample."
                : "No supported wait identified; spare capacity is unknown.";
    return (
        <section
            className="performance-section performance-explanation"
            aria-labelledby="performance-explanation-heading"
        >
            <div className="performance-explanation-heading">
                <h3 id="performance-explanation-heading">Current activity</h3>
                <span
                    className="performance-sample-state"
                    data-current={available}
                >
                    {!diagnosis
                        ? "Unavailable"
                        : !available
                          ? diagnosis.state === "unavailable"
                              ? "Unavailable"
                              : "Stale"
                          : diagnosis.state === "idle"
                            ? "Idle"
                            : diagnosis.state === "observing"
                              ? "Observing"
                              : "Active"}
                </span>
            </div>
            <p className="performance-explanation-summary" role="status">
                {summary}
            </p>
            {diagnosis && (
                <p className="performance-sample-meta">
                    {available
                        ? "Latest sample: "
                        : "Last sample (not current): "}
                    {typeof diagnosis.sampled_at === "number" &&
                    Number.isFinite(diagnosis.sampled_at)
                        ? formatTimestamp(diagnosis.sampled_at)
                        : "Unavailable"}
                    {" · "}
                    {measured(diagnosis.observed_seconds, "s")} observed
                    {" · "}
                    {measured(diagnosis.window_seconds, "s")} window
                </p>
            )}
            {available && diagnosis && (
                <>
                    <p className="performance-workload-scope">
                        In-flight requests · latest sample
                    </p>
                    <dl className="performance-workload">
                        {Object.entries(WORKLOAD).map(([key, label]) => (
                            <div key={key}>
                                <dt>{label}</dt>
                                <dd>
                                    {measured(
                                        diagnosis.workload[
                                            key as keyof typeof WORKLOAD
                                        ],
                                    )}
                                </dd>
                            </div>
                        ))}
                    </dl>
                    {diagnosis.constraints.length > 0 && (
                        <ul className="performance-waits">
                            {diagnosis.constraints.map((constraint, index) => {
                                const wait = WAITS[constraint.code];
                                const primary =
                                    wait &&
                                    constraint.evidence.find(
                                        (item) => item.name === wait.metric,
                                    );
                                return (
                                    <li key={constraint.code + index}>
                                        <details className="performance-wait">
                                            <summary>
                                                <span className="performance-wait-title">
                                                    <strong>
                                                        {wait?.title ||
                                                            "Additional constraint"}
                                                    </strong>
                                                    <span>
                                                        {constraint.certainty ===
                                                        "observed"
                                                            ? "Observed"
                                                            : constraint.certainty ===
                                                                "likely"
                                                              ? "Likely"
                                                              : "Reported"}
                                                    </span>
                                                </span>
                                                {primary && (
                                                    <span className="performance-wait-reading">
                                                        <strong>
                                                            {measured(
                                                                primary.value,
                                                                primary.unit,
                                                            )}
                                                        </strong>
                                                        <span>
                                                            {wait.metricLabel}
                                                        </span>
                                                    </span>
                                                )}
                                            </summary>
                                            {wait && <p>{wait.action}</p>}
                                            <dl className="performance-facts">
                                                {constraint.evidence.map(
                                                    (item, itemIndex) => (
                                                        <div
                                                            key={
                                                                item.name +
                                                                itemIndex
                                                            }
                                                        >
                                                            <dt>
                                                                {EVIDENCE[
                                                                    item.name
                                                                ] || item.name}
                                                            </dt>
                                                            <dd>
                                                                {measured(
                                                                    item.value,
                                                                    item.unit,
                                                                )}
                                                            </dd>
                                                        </div>
                                                    ),
                                                )}
                                            </dl>
                                            <p className="settings-help">
                                                Reported code: {constraint.code}
                                                . Scope: latest sample.
                                            </p>
                                        </details>
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </>
            )}
            <details className="performance-detail-note">
                <summary>Measurements and scope</summary>
                {available && diagnosis && (
                    <dl className="performance-facts">
                        <div>
                            <dt>Telegram inbound · reporting accounts</dt>
                            <dd>
                                {measured(
                                    diagnosis.telegram_inbound_bytes_per_second,
                                    "bytes/s",
                                )}
                            </dd>
                        </div>
                        <div>
                            <dt>HTTP delivered · server-wide</dt>
                            <dd>
                                {measured(
                                    diagnosis.http_delivered_bytes_per_second,
                                    "bytes/s",
                                )}
                            </dd>
                        </div>
                        <div>
                            <dt>Media cache hits · observed window</dt>
                            <dd>{measured(diagnosis.cache_hits)}</dd>
                        </div>
                        <div>
                            <dt>Media cache misses · observed window</dt>
                            <dd>{measured(diagnosis.cache_misses)}</dd>
                        </div>
                    </dl>
                )}
                <p>
                    Counts are in-flight HTTP requests, not users or
                    background-job counts. Pool operations and per-account
                    requests can overlap.
                </p>
                <p>
                    Configured ceilings are policy limits, not a measurement of
                    physical capacity. Stream-buffer pressure does not establish
                    that server RAM is exhausted.
                </p>
                <p>
                    Telegram reads and HTTP delivery measure different parts of
                    the path. Cached delivery can avoid Telegram reads. Low
                    throughput alone cannot locate a network bottleneck. Idle
                    resources can be appropriate when no work can benefit.
                </p>
            </details>
        </section>
    );
}
