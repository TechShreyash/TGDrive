export interface ProgressSample {
    stage: string;
    bytes: number;
    at: number;
    advancedAt: number;
    rate?: number;
}
/** Rate uses observed transport bytes; it never extrapolates stalled progress. */
export function sampleProgress(
    previous: ProgressSample | undefined,
    stage: string,
    bytes: number,
    now: number,
): ProgressSample {
    if (
        !previous ||
        previous.stage !== stage ||
        bytes < previous.bytes ||
        now < previous.at ||
        now - previous.at > 120000
    )
        return { stage, bytes, at: now, advancedAt: now };
    if (bytes === previous.bytes) return previous;
    const seconds = (now - previous.at) / 1000;
    if (seconds < 0.1) return previous;
    const instant = (bytes - previous.bytes) / seconds;
    const weight = 1 - Math.exp(-seconds / 5);
    return {
        stage,
        bytes,
        at: now,
        advancedAt: now,
        rate:
            previous.rate === undefined
                ? instant
                : previous.rate + weight * (instant - previous.rate),
    };
}
export function currentRate(
    sample: ProgressSample | undefined,
    now: number,
): number | undefined {
    return sample?.rate &&
        Number.isFinite(sample.rate) &&
        now - sample.advancedAt < 15000
        ? sample.rate
        : undefined;
}
export function formatDuration(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const whole = Math.floor(seconds);
    if (whole < 60) return whole + "s";
    if (whole < 3600) return Math.floor(whole / 60) + "m " + (whole % 60) + "s";
    if (whole < 86400)
        return (
            Math.floor(whole / 3600) +
            "h " +
            Math.floor((whole % 3600) / 60) +
            "m"
        );
    return (
        Math.floor(whole / 86400) +
        "d " +
        Math.floor((whole % 86400) / 3600) +
        "h"
    );
}

/** Keep small measured transfers visible instead of rounding received data to 0%. */
export function formatProgressPercent(fraction: number): string {
    if (!Number.isFinite(fraction) || fraction <= 0) return "0%";
    const percent = Math.floor(Math.min(1, fraction) * 1000) / 10;
    if (percent === 0) return "<0.1%";
    return (percent < 10 ? percent.toFixed(1) : Math.floor(percent)) + "%";
}

export interface ProgressTransfer {
    state: string;
    kind?: string;
    stage?: string;
    phase: string;
    current: number;
    sentCurrent?: number;
    total: number;
    createdAt?: number;
    startedAt?: number;
    serverStatus?: string;
    finishedAt?: number;
    meter?: ProgressSample;
    error?: string;
    speedBytesPerSecond?: number | null;
    etaSeconds?: number | null;
    progressUpdatedAt?: number | null;
    serverObservedAt?: number;
}
export function transferDetails(item: ProgressTransfer, now: number) {
    const local =
        item.state === "receiving" &&
        !["preparing", "confirming"].includes(item.stage || "");
    const server =
        item.state === "uploading" &&
        item.serverStatus !== "queued" &&
        [
            "saving",
            "uploading",
            "downloading",
            "torrent_uploading",
            "archiving",
            "extracting",
        ].includes(item.stage || "") &&
        item.kind !== "delete";
    const freshServer =
        now - (item.serverObservedAt || 0) < 15000 &&
        (!item.progressUpdatedAt ||
            now - item.progressUpdatedAt * 1000 < 15000);
    const rate = local
        ? currentRate(item.meter, now)
        : server &&
            freshServer &&
            !item.error &&
            item.speedBytesPerSecond &&
            Number.isFinite(item.speedBytesPerSecond) &&
            item.speedBytesPerSecond > 0
          ? item.speedBytesPerSecond
          : undefined;
    const remaining =
        item.total > 0
            ? Math.max(0, item.total - transferProgress(item).current)
            : undefined;
    const serverEta = item.etaSeconds;
    const eta =
        rate && remaining
            ? !local &&
              typeof serverEta === "number" &&
              Number.isFinite(serverEta) &&
              serverEta > 0
                ? serverEta
                : remaining / rate
            : undefined;
    const lastAdvance = local
        ? item.meter?.advancedAt
        : item.progressUpdatedAt
          ? item.progressUpdatedAt * 1000
          : undefined;
    const timing =
        item.state === "uploading" &&
        ["retry_wait", "source_retry_wait", "waiting_for_telegram"].includes(
            item.stage || "",
        )
            ? "waiting"
            : (item.state === "receiving" && item.stage === "confirming") ||
                (item.state === "uploading" &&
                    ["finalizing", "backup_pending"].includes(
                        item.stage || "",
                    )) ||
                ((local || server) && remaining === 0)
              ? "finishing"
              : (local || server) && !rate
                ? (lastAdvance !== undefined && now - lastAdvance >= 15000) ||
                  (server && !freshServer)
                    ? "stalled"
                    : "calculating"
                : undefined;
    const end = item.finishedAt || now;
    return {
        elapsed:
            item.startedAt && Number.isFinite(item.startedAt)
                ? Math.max(0, (end - item.startedAt) / 1000)
                : undefined,
        rate,
        eta: eta && Number.isFinite(eta) && eta > 0 ? eta : undefined,
        measuring: (local || server) && !rate,
        timing,
    };
}

/** The visible bar and byte counter always describe the same transfer stage. */
export function transferProgress(item: ProgressTransfer) {
    const total =
        Number.isFinite(item.total) && item.total > 0 ? item.total : 0;
    const queued =
        item.state === "queued" ||
        (item.state === "uploading" &&
            (item.stage === "queued" || item.serverStatus === "queued"));
    const observed =
        item.state === "receiving" && Number.isFinite(item.sentCurrent)
            ? Math.max(item.current, item.sentCurrent!)
            : item.current;
    const current =
        item.state === "completed"
            ? total
            : queued || !Number.isFinite(observed)
              ? 0
              : Math.max(0, Math.min(observed, total || observed));
    return {
        current,
        total,
        fraction:
            item.state === "completed"
                ? 1
                : total
                  ? Math.min(0.99, current / total)
                  : undefined,
    };
}
