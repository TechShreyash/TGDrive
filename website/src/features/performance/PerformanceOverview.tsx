import { Button } from "@mantine/core";
import { useEffect, useState, useSyncExternalStore } from "react";
import { CircleAlert } from "lucide-react";
import { transfers, isTransferActive } from "../transfers/transfers";
import SettingHelp from "../../shared/SettingHelp";
import type { PerformanceSnapshot } from "./performanceApi";
import { measuredNumber, megabits } from "./performanceReadings";
import "./PerformanceOverview.css";

export function useCurrentPerformance(
    snapshot: PerformanceSnapshot | null,
    error: string,
) {
    const [expired, setExpired] = useState(false);
    useEffect(() => {
        setExpired(false);
        if (!snapshot) return;
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
    return Boolean(snapshot && !expired && !error);
}

export function NetworkReadings({
    snapshot,
    current,
}: {
    snapshot: PerformanceSnapshot;
    current: boolean;
}) {
    const { at, sampled_at: sampledAt, network } = snapshot.server;
    const available =
        current &&
        sampledAt != null &&
        Number.isFinite(sampledAt) &&
        at >= sampledAt &&
        at - sampledAt <= (snapshot.server.stale_after_seconds ?? 15) &&
        network?.available;
    const sent = available
        ? measuredNumber(network.sent_bytes_per_second)
        : null;
    const received = available
        ? measuredNumber(network.received_bytes_per_second)
        : null;
    return (
        <dl
            className="network-readings"
            aria-label="Current server network rates"
        >
            <div>
                <dt className="chart-series-primary">Upload from server</dt>
                <dd data-unavailable={sent === null}>{megabits(sent)}</dd>
            </div>
            <div>
                <dt className="chart-series-secondary">Download to server</dt>
                <dd data-unavailable={received === null}>
                    {megabits(received)}
                </dd>
            </div>
        </dl>
    );
}

export function NetworkMeasurementHelp() {
    return (
        <SettingHelp label="Server network">
            <p>
                Rates include all traffic on the reported interface, including
                Telegram and file delivery. They do not show the entire hosting
                machine’s capacity. Mbps means millions of bits per second.
            </p>
        </SettingHelp>
    );
}

export default function PerformanceOverview({
    snapshot,
    current,
    onAccounts,
    onResources,
}: {
    snapshot: PerformanceSnapshot;
    current: boolean;
    onAccounts: () => void;
    onResources: () => void;
}) {
    const items = useSyncExternalStore(transfers.subscribe, transfers.snapshot);
    const currentItems = items.filter((item) => !item.historyOnly);
    const active = currentItems.filter(
        (item) =>
            isTransferActive(item) &&
            item.state !== "queued" &&
            item.state !== "awaiting_file",
    ).length;
    const queued = currentItems.filter(
        (item) => item.state === "queued",
    ).length;
    const needsAttention = currentItems.filter(
        (item) => item.state === "error" || item.state === "awaiting_file",
    ).length;
    const diagnosis = snapshot.diagnosis;
    const diagnosisAge =
        diagnosis?.sampled_at == null
            ? NaN
            : (snapshot.traffic?.at ?? snapshot.server.at) -
              diagnosis.sampled_at;
    const diagnosisCurrent =
        current &&
        diagnosis &&
        diagnosis.state !== "unavailable" &&
        diagnosisAge >= 0 &&
        diagnosisAge <= diagnosis.stale_after_seconds;
    const constraints = diagnosisCurrent
        ? diagnosis.constraints.filter(
              ({ certainty }) => certainty === "observed",
          )
        : [];
    const limits = (["upload", "download"] as const).flatMap((direction) => {
        const record = snapshot.bandwidth?.[direction];
        if (!current || !record || !record.waiting) return [];
        const configured = measuredNumber(record.configured_bytes_per_second);
        return configured && configured > 0 ? [{ direction, configured }] : [];
    });
    const poolQueued = current ? snapshot.telegram.queued : 0;
    const partQueued = current
        ? snapshot.telegram.accounts.reduce(
              (sum, account) => sum + (account.requests?.queued ?? 0),
              0,
          )
        : 0;
    const cooling = current
        ? snapshot.telegram.accounts.filter(
              (account) =>
                  (poolQueued > 0 || (account.requests?.queued ?? 0) > 0) &&
                  Math.max(
                      account.cooldown_seconds,
                      account.requests?.cooldown_seconds ?? 0,
                  ) > 0,
          ).length
        : 0;
    const reportedTelegramQueue = constraints.find(
        ({ code }) => code === "telegram_queue",
    );
    const reportedTelegramPause = constraints.find(
        ({ code }) => code === "telegram_cooldown",
    );
    const readBufferWait = constraints.find(
        ({ code }) => code === "scratch_wait",
    );
    const waitingStreams = measuredNumber(
        readBufferWait?.evidence.find(({ name }) => name === "waiting_heads")
            ?.value,
    );
    const otherWaits = constraints.some(
        ({ code }) =>
            !["telegram_queue", "telegram_cooldown", "scratch_wait"].includes(
                code,
            ),
    );
    const telegramReadings = [
        poolQueued > 0
            ? `${poolQueued} ${poolQueued === 1 ? "operation" : "operations"} queued`
            : partQueued > 0
              ? `${partQueued} part ${partQueued === 1 ? "request" : "requests"} queued`
              : reportedTelegramQueue
                ? "Queue reported in the latest reading"
                : "",
        cooling > 0
            ? `${cooling} ${cooling === 1 ? "account" : "accounts"} on hold`
            : reportedTelegramPause
              ? "Cooldown reported while work was queued"
              : "",
    ]
        .filter(Boolean)
        .join(" · ");
    const hasWait =
        telegramReadings || limits.length > 0 || readBufferWait || otherWaits;
    const quiet =
        current &&
        diagnosisCurrent &&
        diagnosis?.state !== "observing" &&
        !hasWait;
    const idle = needsAttention === 0 && active === 0 && queued === 0;

    return (
        <div className="performance-dashboard" data-quiet={quiet || undefined}>
            <section
                className="performance-attention"
                aria-labelledby="performance-activity-heading"
            >
                <div className="performance-transfer-summary">
                    <h3
                        id="performance-activity-heading"
                        className={idle ? "performance-idle-label" : undefined}
                    >
                        Transfer activity
                    </h3>
                    <p
                        className={
                            needsAttention > 0
                                ? "performance-transfer-alert"
                                : undefined
                        }
                    >
                        {needsAttention > 0 && (
                            <CircleAlert size={16} aria-hidden="true" />
                        )}
                        <strong>
                            {needsAttention === 0
                                ? idle
                                    ? "Transfers idle"
                                    : "Transfers in progress"
                                : needsAttention === 1
                                  ? "1 transfer needs attention"
                                  : `${needsAttention} transfers need attention`}
                        </strong>
                    </p>
                    {!idle && (
                        <p>
                            {active} active · {queued} queued
                        </p>
                    )}
                    {quiet && (
                        <p className="performance-phone-waits">
                            No requests waiting
                        </p>
                    )}
                </div>
                <Button
                    component="a"
                    className="secondary-button performance-next-action"
                    href="/?view=activity"
                >
                    Open Transfers
                </Button>
            </section>

            <section
                className="performance-waits"
                aria-labelledby="performance-waits-heading"
            >
                <h3 id="performance-waits-heading">Current waits</h3>
                {hasWait ? (
                    <ul className="performance-wait-list">
                        {telegramReadings && (
                            <li>
                                <div>
                                    <h4>Telegram</h4>
                                    <p>{telegramReadings}</p>
                                </div>
                                <Button
                                    type="button"
                                    className="secondary-button performance-next-action"
                                    onClick={onAccounts}
                                >
                                    View Accounts
                                </Button>
                            </li>
                        )}
                        {limits.length > 0 && (
                            <li>
                                <div>
                                    <h4>Configured speed limits</h4>
                                    {limits.map(({ direction, configured }) => (
                                        <p key={direction}>
                                            {direction === "upload"
                                                ? "Server uploads"
                                                : "Server downloads"}
                                            {" waiting at " +
                                                megabits(configured)}
                                        </p>
                                    ))}
                                </div>
                                <Button
                                    component="a"
                                    className="secondary-button performance-next-action"
                                    href="/?view=settings&section=transfers"
                                >
                                    Transfer limits
                                </Button>
                            </li>
                        )}
                        {readBufferWait && (
                            <li>
                                <div>
                                    <h4>Stream read buffers</h4>
                                    <p>
                                        {waitingStreams === null ||
                                        waitingStreams === 0
                                            ? "Streams waiting for a read buffer"
                                            : `${waitingStreams} ${waitingStreams === 1 ? "stream" : "streams"} waiting for a read buffer`}
                                    </p>
                                </div>
                                <Button
                                    type="button"
                                    className="secondary-button performance-next-action"
                                    onClick={onResources}
                                >
                                    Server resources
                                </Button>
                            </li>
                        )}
                        {otherWaits && (
                            <li>
                                <div>
                                    <h4>Other service wait</h4>
                                    <p>
                                        The latest reading did not identify the
                                        source.
                                    </p>
                                </div>
                            </li>
                        )}
                    </ul>
                ) : (
                    <p>
                        {!diagnosisCurrent
                            ? "Current wait readings unavailable. This page checks again automatically."
                            : diagnosis?.state === "observing"
                              ? "Collecting data about current waits."
                              : "No service waits observed."}
                    </p>
                )}
            </section>
        </div>
    );
}
