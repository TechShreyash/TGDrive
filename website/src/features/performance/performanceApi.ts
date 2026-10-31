import { request } from "../../shared/api";

export const MIB = 1024 ** 2;
export const GIB = 1024 ** 3;
/** Owner choices; cache, storage and transfer limits are tuned automatically. */
export interface PerformancePolicy {
    timezone: string;
    auto_preload: boolean;
    upload_split_mib: number;
}
export interface PerformancePreferences extends PerformancePolicy {
    revision: string;
    documentation_url?: string;
    defaults?: PerformancePolicy;
}
export type NumericPreference = Exclude<
    keyof PerformancePolicy,
    "timezone" | "auto_preload"
>;
export const numericPreferences: {
    key: NumericPreference;
    label: string;
    unit: number;
    min: number;
    max?: number;
    appDefault: number;
    unitLabel?: string;
    help: string;
    advanced?: boolean;
}[] = [
    {
        key: "upload_split_mib",
        label: "File split size (MiB)",
        unit: 1,
        min: 0,
        max: 2000,
        appDefault: 2000,
        unitLabel: "MiB",
        help: "Default and maximum: 2000 MiB per Telegram document for every account. Choose a smaller size to split sooner; 0 uses 2000 MiB. Existing transfers keep their plan.",
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
    return category === "cache" ? ["auto_preload"] : ["upload_split_mib"];
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
export function performanceDraft(saved: PerformancePolicy): PerformanceDraft {
    return Object.fromEntries([
        ...numericPreferences.map(({ key, unit }) => [
            key,
            String((saved[key] ?? 0) / unit),
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
    const baseline = performanceDraft(saved);
    for (const { key, label, unit, min, max } of numericPreferences) {
        const value = Number(draft[key]);
        if (
            !draft[key].trim() ||
            !Number.isFinite(value) ||
            value < min ||
            (max !== undefined && value > max) ||
            (unit === 1 && !Number.isInteger(value))
        )
            throw new Error(
                label +
                    " must be " +
                    (unit === 1 ? "a whole number " : "a number ") +
                    (max === undefined
                        ? "of at least " + min
                        : "between " + min + " and " + max) +
                    ".",
            );
        const converted = Math.round(value * unit);
        if (!Number.isSafeInteger(converted))
            throw new Error(label + " is too large.");
        if (converted !== Math.round(Number(baseline[key]) * unit))
            changes[key] = converted;
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
    last_download?: number;
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
    tuning?: {
        running: boolean;
        values: Record<string, number>;
        reasons: Record<string, string>;
    } | null;
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
                signal,
                timeoutMs: 15000,
            },
        ),
    settings: (signal?: AbortSignal) =>
        request<PerformancePreferences>(
            "/api/v1/settings/performance",
            undefined,
            {
                signal,
            },
        ),
    save: (changes: Partial<PerformancePreferences>, revision: string) =>
        request<PerformancePreferences>(
            "/api/v1/settings/performance",
            changes,
            {
                method: "PATCH",
                headers: { "If-Match": revision },
            },
        ),
    preloads: (signal?: AbortSignal) =>
        request<PreloadSnapshot>("/api/v1/preloads", undefined, { signal }),
    prepare: (path: string) =>
        request<PreloadJob>("/api/v1/preloads", { path }),
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
        request<PerformanceSnapshot>("/api/v1/performance", undefined, {
            signal,
        }),
};
