import { request } from "./api";

export const MIB = 1024 ** 2;
export const GIB = 1024 ** 3;
export interface PerformancePreferences {
  revision: string;
  documentation_url?: string;
  timezone: string;
  cache_memory_bytes: number;
  cache_disk_bytes: number;
  cache_min_free_bytes: number;
  cache_ttl_seconds: number;
  auto_preload: boolean;
  preload_max_jobs: number;
  telegram_upload_pipeline: number;
  telegram_request_limit: number;
  telegram_parts_per_second: number;
  telegram_media_per_bot: number;
  telegram_max_queue: number;
  upload_split_mib: number;
}
export type NumericPreference = Exclude<
  keyof PerformancePreferences,
  "revision" | "documentation_url" | "timezone" | "auto_preload"
>;
export const numericPreferences: {
  key: NumericPreference;
  label: string;
  unit: number;
  min: number;
  max: number;
  appDefault: number;
  unitLabel?: string;
  help: string;
  advanced?: boolean;
}[] = [
  {
    key: "cache_memory_bytes",
    label: "Memory cache (MiB)",
    unit: MIB,
    min: 0,
    max: 8192,
    appDefault: 256,
    unitLabel: "MiB",
    help: "Maximum retained media in RAM, reduced automatically when server or container memory is scarce. Idle blocks expire after 10 minutes. 0 disables RAM retention.",
  },
  {
    key: "cache_disk_bytes",
    label: "Disk cache (GiB)",
    unit: GIB,
    min: 0,
    max: 2048,
    appDefault: 10,
    unitLabel: "GiB",
    help: "Maximum disposable media stored on disk, allocated as content arrives. Increase only when space remains for uploads and the system. 0 disables disk caching; originals stay in Telegram.",
  },
  {
    key: "cache_min_free_bytes",
    label: "Cache free-space floor (GiB)",
    unit: GIB,
    min: 0,
    max: 2048,
    appDefault: 1,
    unitLabel: "GiB",
    help: "Cache writes stop before free space falls below this amount. This does not allocate space or restrict other disk users. Upload staging has its own reserve.",
  },
  {
    key: "cache_ttl_seconds",
    label: "Disk cache retention (hours)",
    unit: 3600,
    min: 60 / 3600,
    max: 744,
    appDefault: 24,
    unitLabel: "hours",
    help: "Expire disk blocks after this much time without reuse; each read refreshes the timer. Capacity pressure can evict blocks sooner. Longer retention helps repeated viewing when space allows.",
  },
  {
    key: "preload_max_jobs",
    label: "Concurrent preloads",
    unit: 1,
    min: 1,
    max: 8,
    appDefault: 2,
    help: "Combined manual and automatic preparation jobs. Active playback and downloads take priority. More jobs share existing account capacity; they do not add it.",
  },
  {
    key: "upload_split_mib",
    label: "File split size (MiB)",
    unit: 1,
    min: 0,
    max: 2000,
    appDefault: 2000,
    unitLabel: "MiB",
    help: "0 uses the safe account maximum, including an eligible Premium document. Choose 1–2000 MiB for smaller parts; existing transfers keep their plan.",
  },
  {
    key: "telegram_upload_pipeline",
    label: "Upload pipeline",
    unit: 1,
    min: 1,
    max: 16,
    appDefault: 4,
    help: "Concurrent 512 KiB part-upload requests within one Telegram document, also bounded by the shared account request limit. Raise gradually only if throughput improves without cooldowns.",
    advanced: true,
  },
  {
    key: "telegram_request_limit",
    label: "Requests per account",
    unit: 1,
    min: 1,
    max: 32,
    appDefault: 8,
    help: "Concurrent Telegram requests per account, shared by uploads, downloads and media metadata. Higher values can compete with playback and trigger cooldowns.",
    advanced: true,
  },
  {
    key: "telegram_parts_per_second",
    label: "Requests per second",
    unit: 1,
    min: 1,
    max: 128,
    appDefault: 48,
    help: "Application pacing ceiling per account. Telegram cooldowns always take precedence.",
    advanced: true,
  },
  {
    key: "telegram_media_per_bot",
    label: "Media operations per account",
    unit: 1,
    min: 1,
    max: 16,
    appDefault: 3,
    help: "Concurrent Telegram upload/download operations per account, including missing-block reads. Per-bot and Premium upload limits also apply. This is not a viewer limit; cached payloads need no Telegram download slot.",
    advanced: true,
  },
  {
    key: "telegram_max_queue",
    label: "Maximum queued operations",
    unit: 1,
    min: 1,
    max: 4096,
    appDefault: 256,
    help: "Maximum waiting Telegram origin operations across the server, not whole files or viewers. Excess work receives retry guidance. A larger queue permits longer waits; it does not increase speed.",
    advanced: true,
  },
];
export type PerformanceDraft = Record<NumericPreference, string> & {
  timezone: string;
  auto_preload: boolean;
};
export type PerformanceCategory = "cache" | "transfers" | "regional";
export function performanceCategoryKeys(
  category: PerformanceCategory,
): (keyof PerformanceDraft)[] {
  if (category === "regional") return ["timezone"];
  return [
    ...numericPreferences
      .filter(({ key }) =>
        category === "cache"
          ? key.startsWith("cache_") || key === "preload_max_jobs"
          : key.startsWith("telegram_") || key === "upload_split_mib",
      )
      .map(({ key }) => key),
    ...(category === "cache" ? ["auto_preload" as const] : []),
  ];
}
export function performanceCategoryDraft(
  draft: PerformanceDraft,
  saved: PerformancePreferences,
  category: PerformanceCategory,
): PerformanceDraft {
  const result = performanceDraft(saved);
  for (const key of performanceCategoryKeys(category))
    Object.assign(result, { [key]: draft[key] });
  return result;
}
export function performanceDraft(
  saved: PerformancePreferences,
): PerformanceDraft {
  return Object.fromEntries([
    ...numericPreferences.map(({ key, unit }) => [
      key,
      String(saved[key] / unit),
    ]),
    ["timezone", saved.timezone],
    ["auto_preload", saved.auto_preload],
  ]) as PerformanceDraft;
}
export function performanceChanges(
  draft: PerformanceDraft,
  saved: PerformancePreferences,
): Partial<PerformancePreferences> {
  const changes: Partial<PerformancePreferences> = {};
  for (const { key, label, unit, min, max } of numericPreferences) {
    const value = Number(draft[key]);
    if (
      !draft[key].trim() ||
      !Number.isFinite(value) ||
      value < min ||
      value > max ||
      (unit === 1 && !Number.isInteger(value))
    )
      throw new Error(
        label +
          " must be " +
          (unit === 1 ? "a whole number " : "a number ") +
          "between " +
          min +
          " and " +
          max +
          ".",
      );
    const converted = Math.round(value * unit);
    if (!Number.isSafeInteger(converted))
      throw new Error(label + " is too large.");
    if (converted !== saved[key]) changes[key] = converted;
  }
  const timezone = draft.timezone.trim();
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone }).format();
  } catch {
    throw new Error("Use a valid timezone, such as Asia/Kolkata.");
  }
  if (timezone !== saved.timezone) changes.timezone = timezone;
  if (draft.auto_preload !== saved.auto_preload)
    changes.auto_preload = draft.auto_preload;
  return changes;
}
export interface CachedFile {
  id: string;
  name: string;
  size: number;
  ready_bytes: number;
  complete: boolean;
  ready_ranges: [number, number][];
  memory_ranges?: [number, number][];
  disk_ranges?: [number, number][];
}
export interface ScratchSnapshot {
  bytes: number;
  max_bytes: number;
  configured_max_bytes: number;
  peak_bytes: number;
  waiting_heads: number;
  active_streams: number;
}
export interface CacheSnapshot {
  scratch?: ScratchSnapshot;
  memory: {
    bytes: number;
    max_bytes: number;
    effective_max_bytes: number;
    hits: number;
    misses: number;
    entries?: number;
  };
  disk: {
    available: boolean;
    bytes: number;
    max_bytes: number;
    free_bytes?: number | null;
    min_free_bytes?: number;
    hits?: number;
    misses?: number;
    evictions?: number;
    write_failures?: number;
    entries?: number;
  };
  files: CachedFile[];
  pending_writes: number;
}
export interface PreloadJob {
  id: string;
  file_id: string;
  name: string;
  state: string;
  mode: string;
  bytes_done: number;
  target_bytes: number;
  size: number;
  start: number;
  end: number;
  ready_bytes?: number;
  complete?: boolean;
  error?: string | null;
}
export interface PreloadSnapshot {
  jobs: PreloadJob[];
  active_jobs: number;
  max_background_jobs: number;
  cache: CacheSnapshot;
}
export interface LoadSample {
  at: number;
  active: number;
  queued: number;
}
export interface QueueEntry {
  position: number;
  priority: string;
  operation: string;
  waiting_seconds: number;
  estimated_wait_seconds: number | null;
  request_id?: string;
  client_name?: string | null;
}
export interface AccountPerformance {
  name: string;
  kind: string;
  uploads: number;
  downloads: number;
  combined_limit: number;
  cooldown_seconds: number;
  requests?: {
    active: number;
    queued: number;
    configured_limit: number;
    effective_limit: number;
    configured_parts_per_second: number;
    effective_parts_per_second: number;
    floods: number;
    failed: number;
    completed: number;
    bytes: number;
    cooldown_seconds?: number;
    upload_bytes?: number;
    download_bytes?: number;
  } | null;
}
export interface PerformanceSnapshot {
  traffic?: TrafficSample & {
    started_at: number;
    history: TrafficSample[];
    download_bytes_per_second?: number | null;
    upload_bytes_per_second?: number | null;
  };
  telegram: {
    active: number;
    queued: number;
    max_queue: number;
    estimates: boolean;
    mean_activity_seconds: number | null;
    queue: QueueEntry[];
    accounts: AccountPerformance[];
    history: LoadSample[];
    active_requests?: {
      request_id: string;
      client_name: string;
      priority: string;
      operation: string;
      elapsed_seconds: number;
    }[];
  };
  cache: CacheSnapshot;
  server: {
    history?: ResourceSample[];
    at: number;
    memory: Record<string, number | null>;
    process_peak_rss_bytes: number;
    process_rss_bytes?: number | null;
    cpu_count?: number;
    cpu?: {
      percent: number | null;
      scope: "cgroup" | "host" | null;
      capacity_cores?: number;
    };
    network?: {
      available: boolean;
      interface: string | null;
      started_at: number;
      received_bytes: number;
      sent_bytes: number;
      received_bytes_per_second: number | null;
      sent_bytes_per_second: number | null;
      scope: string;
    };
    load_average: number[];
    disk: { total_bytes: number; free_bytes: number; used_bytes: number };
    staging_budget_bytes: number;
  };
  drive: { files: number; folders: number; total_bytes: number };
  history_scope?: string;
}
export interface ResourceSample {
  at: number;
  end_at?: number;
  cpu_percent: number | null;
  cpu_scope: "cgroup" | "host" | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  received_bytes_per_second: number | null;
  sent_bytes_per_second: number | null;
  network_interface: string | null;
  coverage_seconds?: Record<string, number>;
}
export interface PerformanceHistory {
  start: number;
  end: number;
  bucket_seconds: number;
  samples: ResourceSample[];
  retention_seconds: number;
  earliest_at: number | null;
  tracked_since: number | null;
  last_updated?: number | null;
  available: boolean;
  totals: {
    network_received_bytes: number | null;
    network_sent_bytes: number | null;
    http_upload_bytes: number | null;
    http_download_bytes: number | null;
    http_requests: number | null;
    http_errors: number | null;
    telegram_upload_bytes?: number | null;
    telegram_download_bytes?: number | null;
  };
}
export interface TrafficSample {
  at: number;
  requests: number;
  active: number;
  download_bytes: number;
  upload_bytes: number;
  errors: number;
}
export const performanceApi = {
  history: (start: number, end: number, signal?: AbortSignal) =>
    request<PerformanceHistory>(
      "/api/v1/performance/history?" +
        new URLSearchParams({ start: String(start), end: String(end) }),
      undefined,
      {
        signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(15000)])
          : AbortSignal.timeout(15000),
      },
    ),
  settings: (signal?: AbortSignal) =>
    request<PerformancePreferences>("/api/v1/settings/performance", undefined, {
      signal,
    }),
  save: (changes: Partial<PerformancePreferences>, revision: string) =>
    request<PerformancePreferences>("/api/v1/settings/performance", changes, {
      method: "PATCH",
      headers: { "If-Match": revision },
    }),
  preloads: (signal?: AbortSignal) =>
    request<PreloadSnapshot>("/api/v1/preloads", undefined, { signal }),
  prepare: (path: string) => request<PreloadJob>("/api/v1/preloads", { path }),
  clear: (fileId?: string) =>
    request<{ status: string }>(
      "/api/v1/preloads/clear",
      fileId ? { file_id: fileId } : {},
    ),
  cancel: (id: string) =>
    request<{ status: string }>(
      "/api/v1/preloads/" + encodeURIComponent(id) + "/cancel",
      {},
    ),
  snapshot: (signal?: AbortSignal) =>
    request<PerformanceSnapshot>("/api/v1/performance", undefined, { signal }),
};
