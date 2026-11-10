import { useEffect, useState } from "react";
import { formatBytes, formatTimestamp } from "../../shared/types";
import type { PerformanceDiagnosis } from "./performanceApi";

const WAITS: Record<string, { title: string; action: string }> = {
    telegram_queue: {
        title: "Work is waiting for Telegram admission",
        action: "Check Accounts for queue and capacity details. Configured ceilings are policy limits, not a measurement of physical capacity.",
    },
    telegram_cooldown: {
        title: "Queued work overlaps a Telegram cooldown",
        action: "Wait for the reported cooldown, then recheck Accounts.",
    },
    scratch_wait: {
        title: "Reads are waiting for stream-buffer memory",
        action: "Let current reads finish or reduce simultaneous reads. Compare reserved bytes with the effective stream-buffer limit below.",
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
    playback_requests: "Playback requests",
    download_requests: "Download requests",
    unknown_file_requests: "Unclassified file requests",
    upload_requests: "Device upload requests",
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
        ? "Performance explanations are unavailable from this server. Raw measurements remain available below."
        : !available
          ? "Current performance evidence is unavailable or stale. Refresh Performance to collect a new reading. Raw measurements remain available below."
          : diagnosis.state === "idle"
            ? "No workload activity was observed in this window. Spare resources can be appropriate when no work can benefit."
            : diagnosis.state === "observing"
              ? "Collecting samples. Early readings do not establish a sustained constraint."
              : diagnosis.constraints.length
                ? "Activity was observed with the following reported waits. Multiple waits can occur together."
                : "Activity was observed, with no supported wait identified in the latest sample. This does not establish spare capacity or locate a bottleneck.";
    return (
        <section
            className="performance-section performance-explanation"
            aria-labelledby="performance-explanation-heading"
        >
            <h3 id="performance-explanation-heading">
                Performance explanation
            </h3>
            <p className="settings-help">{summary}</p>
            {diagnosis && (
                <p className="settings-help">
                    {available
                        ? "Latest sample: "
                        : "Last sample (not current): "}
                    {typeof diagnosis.sampled_at === "number" &&
                    Number.isFinite(diagnosis.sampled_at)
                        ? formatTimestamp(diagnosis.sampled_at)
                        : "Unavailable"}
                    . Observed {measured(diagnosis.observed_seconds, "seconds")}{" "}
                    of a {measured(diagnosis.window_seconds, "second")} window.
                </p>
            )}
            {available && diagnosis && (
                <>
                    <dl className="performance-facts">
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
                    <p className="settings-help">
                        Activity counts are in-flight HTTP requests at the
                        latest sample, not users or background-job counts. Rates
                        and cache deltas below cover the observed window.
                    </p>
                    {diagnosis.constraints.map((constraint, index) => (
                        <div
                            className="performance-wait"
                            key={constraint.code + index}
                        >
                            <h4>
                                {constraint.certainty === "observed"
                                    ? "Observed: "
                                    : constraint.certainty === "likely"
                                      ? "Likely explanation: "
                                      : "Reported: "}
                                {WAITS[constraint.code]?.title ||
                                    "Additional constraint"}
                            </h4>
                            {WAITS[constraint.code] && (
                                <p>{WAITS[constraint.code].action}</p>
                            )}
                            <details className="performance-detail-note">
                                <summary>Evidence for this wait</summary>
                                <p>
                                    Reported code: {constraint.code}. Scope:
                                    latest sample.
                                </p>
                                <dl className="performance-facts">
                                    {constraint.evidence.map(
                                        (item, itemIndex) => (
                                            <div key={item.name + itemIndex}>
                                                <dt>
                                                    {EVIDENCE[item.name] ||
                                                        item.name}
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
                            </details>
                        </div>
                    ))}
                </>
            )}
            <details className="performance-detail-note">
                <summary>Measurements and interpretation</summary>
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
                    Pool operations and per-account requests are different
                    admission layers; their queue counts can overlap.
                    Stream-buffer limits are admission budgets, not evidence
                    that system RAM is exhausted.
                </p>
                <p>
                    Useful throughput and responsive playback matter more than
                    filling CPU or memory. Idle resources can be appropriate
                    when no work can benefit.
                </p>
                <p>
                    Telegram reads and HTTP delivery measure different parts of
                    the path. Cached bytes can be delivered without another
                    Telegram read. Low throughput alone cannot locate a network
                    bottleneck, and delivery backpressure does not identify the
                    client as its cause.
                </p>
            </details>
        </section>
    );
}
