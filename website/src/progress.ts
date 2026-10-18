export interface ProgressSample {
  stage: string;
  bytes: number;
  at: number;
  advancedAt: number;
  rate?: number;
}
/** Rate uses acknowledged bytes; it never counts bytes merely handed to fetch. */
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
      Math.floor(whole / 3600) + "h " + Math.floor((whole % 3600) / 60) + "m"
    );
  return (
    Math.floor(whole / 86400) + "d " + Math.floor((whole % 86400) / 3600) + "h"
  );
}

export interface ProgressTransfer {
  state: string;
  kind?: string;
  stage?: string;
  phase: string;
  current: number;
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
  const local = item.state === "receiving";
  const server =
    item.state === "uploading" &&
    item.serverStatus !== "queued" &&
    ["saving", "uploading", "downloading"].includes(item.stage || "") &&
    item.kind !== "delete";
  const freshServer =
    now - (item.serverObservedAt || 0) < 15000 &&
    (!item.progressUpdatedAt || now - item.progressUpdatedAt * 1000 < 15000);
  const rate = local
    ? currentRate(item.meter, now)
    : server &&
        freshServer &&
        !item.error &&
        item.speedBytesPerSecond &&
        Number.isFinite(item.speedBytesPerSecond)
      ? item.speedBytesPerSecond
      : undefined;
  const remaining =
    item.total > 0 ? Math.max(0, item.total - item.current) : undefined;
  const eta = rate && remaining && (local ? remaining / rate : item.etaSeconds);
  const end = item.finishedAt || now;
  return {
    elapsed:
      item.startedAt && Number.isFinite(item.startedAt)
        ? Math.max(0, (end - item.startedAt) / 1000)
        : undefined,
    rate,
    eta: eta && Number.isFinite(eta) && eta > 0 ? eta : undefined,
    measuring: (local || server) && !rate,
  };
}

/** The visible bar and byte counter always describe the same transfer stage. */
export function transferProgress(item: ProgressTransfer) {
  const total = Number.isFinite(item.total) && item.total > 0 ? item.total : 0;
  const queued =
    item.state === "queued" ||
    (item.state === "uploading" &&
      (item.stage === "queued" || item.serverStatus === "queued"));
  const current =
    item.state === "completed"
      ? total
      : queued || !Number.isFinite(item.current)
        ? 0
        : Math.max(0, Math.min(item.current, total || item.current));
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
