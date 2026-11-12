import { useEffect, useState, useSyncExternalStore } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { formatTimestamp } from "../../shared/types";
import { transfers, isTransferActive } from "../transfers/transfers";
import SettingHelp from "../../shared/SettingHelp";
import type { PerformanceSnapshot } from "./performanceApi";
import { measuredNumber, megabits } from "./performanceReadings";
import "./PerformanceOverview.css";
const WAIT_LABELS: Record<string, string> = {
    telegram_queue: "Requests are waiting for Telegram",
    telegram_cooldown: "Telegram has requested a pause",
    scratch_wait: "Streams are waiting for a read buffer",
};

export default function PerformanceOverview({
    snapshot,
    error,
}: {
    snapshot: PerformanceSnapshot;
    error: string;
}) {
    const items = useSyncExternalStore(transfers.subscribe, transfers.snapshot);
    const currentItems = items.filter((item) => !item.historyOnly);
    const activeTransfers = currentItems.filter(
        (item) =>
            isTransferActive(item) &&
            item.state !== "queued" &&
            item.state !== "awaiting_file",
    ).length;
    const queuedTransfers = currentItems.filter(
        (item) => item.state === "queued",
    ).length;
    const attentionTransfers = currentItems.filter(
        (item) => item.state === "error" || item.state === "awaiting_file",
    ).length;
    const [expired, setExpired] = useState(false);
    useEffect(() => {
        setExpired(false);
        const age =
            snapshot.server.sampled_at == null
                ? 0
                : Math.max(0, snapshot.server.at - snapshot.server.sampled_at);
        const timer = setTimeout(
            () => setExpired(true),
            Math.max(0, (snapshot.server.stale_after_seconds ?? 30) - age) *
                1000,
        );
        return () => clearTimeout(timer);
    }, [snapshot]);
    const current = Boolean(snapshot && !expired && !error);
    const network = snapshot?.server.network;
    const telemetryCurrent =
        current &&
        snapshot?.server.sampled_at != null &&
        Number.isFinite(snapshot.server.sampled_at) &&
        snapshot.server.at >= snapshot.server.sampled_at &&
        snapshot.server.at - snapshot.server.sampled_at <=
            (snapshot.server.stale_after_seconds ?? 15);
    const sent =
        telemetryCurrent && network?.available
            ? measuredNumber(network.sent_bytes_per_second)
            : null;
    const received =
        telemetryCurrent && network?.available
            ? measuredNumber(network.received_bytes_per_second)
            : null;
    const diagnosis = snapshot?.diagnosis;
    const diagnosisAge =
        diagnosis?.sampled_at == null || !snapshot
            ? NaN
            : (snapshot.traffic?.at ?? snapshot.server.at) -
              diagnosis.sampled_at;
    const diagnosisCurrent =
        current &&
        diagnosis &&
        diagnosis.state !== "unavailable" &&
        Number.isFinite(diagnosisAge) &&
        diagnosisAge >= 0 &&
        diagnosisAge <= diagnosis.stale_after_seconds;
    const constraints = diagnosisCurrent ? diagnosis.constraints : [];
    const bandwidthWaits = (["upload", "download"] as const).filter(
        (direction) => {
            const value = snapshot?.bandwidth?.[direction];
            return (
                current &&
                value &&
                measuredNumber(value.configured_bytes_per_second) !== null &&
                value.configured_bytes_per_second! > 0 &&
                value.waiting > 0
            );
        },
    );
    return (
        <div className="performance-dashboard">
            <p className="performance-sample-time">
                {current ? "Latest snapshot: " : "Last loaded snapshot: "}
                {formatTimestamp(
                    snapshot.server.sampled_at ?? snapshot.server.at,
                )}
            </p>
            <section aria-labelledby="performance-activity-heading">
                <h3 id="performance-activity-heading">Current activity</h3>
                <p className="muted">
                    {current
                        ? snapshot.telegram.active +
                          " active Telegram operations · " +
                          snapshot.telegram.queued +
                          " waiting"
                        : "Current activity reading unavailable"}
                    <SettingHelp label="Server network">
                        <p>
                            These are measured interface counters, not
                            upload-admission estimates. They can include
                            Telegram, file delivery and other traffic on that
                            interface. They do not establish the physical
                            hosting machine's total traffic or capacity. Mbps
                            means millions of bits per second.
                        </p>
                    </SettingHelp>
                </p>
                <dl className="network-readings">
                    <div>
                        <dt>
                            <ArrowUp size={18} aria-hidden="true" /> Upload from
                            server
                        </dt>
                        <dd data-unavailable={sent === null}>
                            {megabits(sent)}
                        </dd>
                    </div>
                    <div>
                        <dt>
                            <ArrowDown size={18} aria-hidden="true" /> Download
                            to server
                        </dt>
                        <dd data-unavailable={received === null}>
                            {megabits(received)}
                        </dd>
                    </div>
                </dl>
                <p aria-label="Current Transfers">
                    Transfer manager: {activeTransfers} active ·{" "}
                    {queuedTransfers} queued · {attentionTransfers} need
                    attention
                </p>
                <div className="performance-summary-actions">
                    <a
                        className="inline-link"
                        href="/?view=settings&section=transfers"
                    >
                        Transfer settings
                    </a>
                    <a className="inline-link" href="/?view=activity">
                        Open Transfers
                    </a>
                </div>
            </section>

            <section
                className="performance-waiting"
                aria-labelledby="performance-waits-heading"
            >
                <h3 id="performance-waits-heading">What is waiting</h3>
                {bandwidthWaits.length > 0 && (
                    <ul>
                        {bandwidthWaits.map((direction) => (
                            <li key={direction}>
                                <strong>
                                    {direction === "upload"
                                        ? "Upload from server"
                                        : "Download to server"}
                                </strong>
                                {" is waiting for its configured speed limit (" +
                                    megabits(
                                        snapshot!.bandwidth![direction]
                                            .configured_bytes_per_second,
                                    ) +
                                    ")."}
                                {
                                    " This is intentional throttling, not evidence of a network failure."
                                }
                            </li>
                        ))}
                    </ul>
                )}
                {!diagnosisCurrent ? (
                    <p>
                        Current wait evidence is unavailable. Refresh to check
                        again.
                    </p>
                ) : constraints.length ? (
                    <>
                        <ul>
                            {constraints.map((constraint, index) => (
                                <li key={constraint.code + index}>
                                    <strong>
                                        {WAIT_LABELS[constraint.code] ||
                                            "Additional reported constraint"}
                                    </strong>
                                    {" · " +
                                        (constraint.certainty === "observed"
                                            ? "Observed"
                                            : constraint.certainty === "likely"
                                              ? "Likely"
                                              : "Reported")}
                                </li>
                            ))}
                        </ul>
                        <p>
                            A reported wait does not establish that the entire
                            server is at capacity.
                        </p>
                    </>
                ) : (
                    <p>
                        {diagnosis?.state === "observing"
                            ? "Collecting observations. No wait reported yet."
                            : bandwidthWaits.length > 0
                              ? "No additional Telegram or stream-buffer wait reported."
                              : "All clear — no delays detected in the latest check."}
                    </p>
                )}
                {constraints.some((item) =>
                    item.code.startsWith("telegram_"),
                ) && (
                    <a className="inline-link" href="/?view=connections">
                        Check Telegram connections
                    </a>
                )}
            </section>
        </div>
    );
}
