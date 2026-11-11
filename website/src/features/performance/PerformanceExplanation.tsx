import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { formatTimestamp } from "../../shared/types";
import {
    resourceBytes as formatBytes,
    type PerformanceDiagnosis,
} from "./performanceApi";

const WAITS: Record<
    string,
    { title: string; action: string; metric: string; metricLabel: string }
> = {
    telegram_queue: {
        title: "Waiting for Telegram",
        metric: "pool_queued_operations",
        metricLabel: "Waiting operations",
        action: "Open Accounts to inspect queues and configured limits.",
    },
    telegram_cooldown: {
        title: "Telegram asked us to pause",
        metric: "longest_cooldown_seconds",
        metricLabel: "Pause remaining",
        action: "Wait for the reported cooldown, then recheck Accounts.",
    },
    scratch_wait: {
        title: "Waiting for a stream buffer",
        metric: "waiting_heads",
        metricLabel: "Streams waiting",
        action: "Let current reads finish or reduce simultaneous streams.",
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
    playback_requests: "Streaming",
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
    liveRates,
    telegram,
    onAccounts,
}: {
    diagnosis?: PerformanceDiagnosis | null;
    serverAt?: number;
    liveRates?: { toViewers: number | null; fromDevices: number | null };
    telegram?: { active: number; queued: number };
    onAccounts?: () => void;
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
    const activeWorkload = diagnosis
        ? Object.entries(WORKLOAD).filter(
              ([key]) => diagnosis.workload[key as keyof typeof WORKLOAD] > 0,
          )
        : [];
    const activeOperations =
        (telegram?.active ?? 0) + (telegram?.queued ?? 0) > 0;
    const liveWork = activeWorkload.length > 0 || activeOperations;
    const measuredActivity =
        liveWork ||
        (liveRates?.toViewers ?? 0) > 0 ||
        (liveRates?.fromDevices ?? 0) > 0;
    const currentState = !diagnosis
        ? "unavailable"
        : !available
          ? diagnosis.state === "unavailable"
              ? "unavailable"
              : "stale"
          : diagnosis.state === "idle" && measuredActivity
            ? "active"
            : diagnosis.state;
    const summary = !available
        ? "Current information is unavailable. Refresh to try again."
        : currentState === "idle"
          ? "Nothing is transferring right now."
          : currentState === "observing"
            ? "Checking recent activity..."
            : diagnosis!.constraints.length
              ? "A transfer is waiting. See the reason below."
              : measuredActivity
                ? "Transfers are running; no measured wait reported."
                : "Nothing is transferring in the latest reading.";
    const state =
        currentState === "unavailable"
            ? "Unavailable"
            : currentState === "stale"
              ? "Out of date"
              : currentState === "observing"
                ? "Checking"
                : currentState === "idle"
                  ? "Idle"
                  : measuredActivity
                    ? "Active"
                    : "Recent activity";
    const deliveryRate = measured(liveRates?.toViewers, "bytes/s");
    const uploadRate = measured(liveRates?.fromDevices, "bytes/s");
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
                    data-state={currentState}
                >
                    {state}
                </span>
            </div>
            <p className="performance-explanation-summary" role="status">
                {summary}
            </p>

            {liveRates && (
                <dl className="performance-file-rates">
                    <div>
                        <dt>
                            <ArrowDown size={18} aria-hidden="true" />
                            <span>Streaming and downloads</span>
                        </dt>
                        <dd data-unavailable={deliveryRate === "Unavailable"}>
                            {deliveryRate}
                        </dd>
                    </div>
                    <div>
                        <dt>
                            <ArrowUp size={18} aria-hidden="true" />
                            <span>Device uploads</span>
                        </dt>
                        <dd data-unavailable={uploadRate === "Unavailable"}>
                            {uploadRate}
                        </dd>
                    </div>
                </dl>
            )}
            {available && diagnosis && (
                <>
                    {activeWorkload.length > 0 && (
                        <p className="performance-workload-scope">
                            {activeWorkload
                                .map(([, label]) => label)
                                .join(" / ")}
                        </p>
                    )}
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
                                        {wait && (
                                            <p className="performance-wait-action">
                                                {wait.action}
                                            </p>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                    {onAccounts &&
                        diagnosis.constraints.some(
                            (item) =>
                                item.code === "telegram_queue" ||
                                item.code === "telegram_cooldown",
                        ) && (
                            <button
                                type="button"
                                className="secondary-button"
                                onClick={onAccounts}
                            >
                                View Accounts
                            </button>
                        )}
                </>
            )}
            <details className="performance-detail-note">
                <summary>Measurements and scope</summary>
                {diagnosis && (
                    <p className="performance-sample-meta">
                        {available
                            ? "Latest sample: "
                            : "Last sample (not current): "}
                        {typeof diagnosis.sampled_at === "number" &&
                        Number.isFinite(diagnosis.sampled_at)
                            ? formatTimestamp(diagnosis.sampled_at)
                            : "Unavailable"}
                        {" / "}
                        {measured(diagnosis.observed_seconds, "s")} observed
                        {" / "}
                        {measured(diagnosis.window_seconds, "s")} window
                    </p>
                )}
                {available && activeOperations && (
                    <p>
                        Telegram operations: {telegram!.active} active,{" "}
                        {telegram!.queued} waiting.
                    </p>
                )}
                {available && diagnosis && (
                    <dl className="performance-facts">
                        {Object.entries(WORKLOAD).map(([key, label]) => (
                            <div key={key}>
                                <dt>{label} requests</dt>
                                <dd>
                                    {measured(
                                        diagnosis.workload[
                                            key as keyof typeof WORKLOAD
                                        ],
                                    )}
                                </dd>
                            </div>
                        ))}
                        <div>
                            <dt>Telegram inbound - reporting accounts</dt>
                            <dd>
                                {measured(
                                    diagnosis.telegram_inbound_bytes_per_second,
                                    "bytes/s",
                                )}
                            </dd>
                        </div>
                        <div>
                            <dt>HTTP delivered - server-wide</dt>
                            <dd>
                                {measured(
                                    diagnosis.http_delivered_bytes_per_second,
                                    "bytes/s",
                                )}
                            </dd>
                        </div>
                        <div>
                            <dt>Media cache hits - observed window</dt>
                            <dd>{measured(diagnosis.cache_hits)}</dd>
                        </div>
                        <div>
                            <dt>Media cache misses - observed window</dt>
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
            <div className="performance-activity-actions">
                <a className="secondary-button" href="/?view=activity">
                    View Transfers
                </a>
                <a
                    className="inline-link"
                    href="/?view=settings&section=transfers"
                >
                    Upload storage limits
                </a>
            </div>
        </section>
    );
}
