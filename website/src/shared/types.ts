export interface DriveItem {
    id: string;
    name: string;
    path: string;
    type: "file" | "folder";
    size?: number;
    upload_date?: string;
    parent_names?: string[];
    starred?: boolean;
    read_only?: boolean;
    demo_files?: boolean;
}
export interface Directory {
    name?: string;
    read_only?: boolean;
    contents: Record<string, DriveItem>;
}
export interface TransferLocation {
    available: boolean;
    folder_path?: string;
    folder_label?: string;
    item_id?: string | null;
    item_name?: string | null;
    display_path?: string;
    reason?: string;
}
export interface FolderUploadMetadata {
    id: string;
    name: string;
    root_path: string;
    created_at: number;
}
export interface Job {
    history_cleared?: boolean;
    folder_upload?: FolderUploadMetadata;
    queue?: { position: number; estimated_wait_seconds: number | null } | null;
    telegram_parts?: { total: number; stored: number };
    torrent_files?: { total: number; completed: number };
    location?: TransferLocation;
    id: string;
    status: string;
    phase: string;
    current: number;
    total: number;
    percentage?: number;
    error?: string | null;
    name?: string;
    destination?: string;
    kind?: string;
    retry_at?: number | null;
    source_attempt?: number;
    source_max_attempts?: number;
    created_at?: number;
    started_at?: number | null;
    updated_at?: number;
    finished_at?: number | null;
    elapsed_seconds?: number;
    speed_bytes_per_second?: number | null;
    eta_seconds?: number | null;
    progress_updated_at?: number | null;
    download_mode?: "full" | "warm" | "queued" | "waiting_storage";
}
export interface TransferTaskHistoryPage {
    jobs: Job[];
    page: number;
    page_size: number;
    total_tasks: number;
    total_pages: number;
    has_more: boolean;
}
export interface UploadSnapshot {
    folder_upload?: FolderUploadMetadata;
    id: string;
    offset: number;
    total_size: number;
    chunk_size: number;
    expires_at?: string;
    checksums?: { offset: number; size: number; sha256: string }[];
}
export interface Share {
    label?: string;
    id?: string;
    node_id?: string;
    path: string;
    name?: string;
    created_at: string;
    expires_at: string | number | null;
    type?: "file" | "folder";
    size?: number;
    folder_path?: string;
    folder_label?: string;
    copyable?: boolean;
}
export interface PublicConfig {
    demo_data?: boolean;
    demo_mode?: boolean;
    demo_read_only?: boolean;
    demo_admin?: boolean;
    max_file_size: number;
    timezone?: string;
}
export interface DriveProtection {
    backend?: "mongodb";
    backup_error?: string | null;
    backup_key_configured?: boolean;
    recovery_required?: boolean;
    code?: string;
    state: "protected" | "pending" | "error";
    metadata_revision: number;
    protected_revision: number;
    checkpoint_message_id?: number | null;
    last_success_at?: string | null;
    error?: string | null;
}
export interface ServerStatus {
    recovery?: DriveProtection | null;
    telegram_ready?: boolean;
    telegram_issues?: { code: string; detail: string }[];
    admission_paused?: boolean;
    restart_required?: boolean;
    backup?: unknown;
    [key: string]: unknown;
}
export type View =
    | "starred"
    | "files"
    | "trash"
    | "shares"
    | "activity"
    | "connections"
    | "storage"
    | "settings"
    | "preloads"
    | "performance";
export function itemPath(item: DriveItem): string {
    const parent = item.path.endsWith("/") ? item.path.slice(0, -1) : item.path;
    return parent + "/" + item.id;
}
const byteNumberFormat = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 2,
});
export function formatBytes(value: number | undefined): string {
    if (value === undefined || !Number.isFinite(value) || value < 0) return "—";
    if (value < 1024)
        return byteNumberFormat.format(value) + " B";
    const exponent = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
    return (
        byteNumberFormat.format(value / 1024 ** exponent) +
        " " +
        ["B", "KB", "MB", "GB", "TB"][exponent]
    );
}
let instanceTimezone = "Asia/Kolkata";
export function getTimezone(): string {
    return instanceTimezone;
}
export function setTimezone(value: string) {
    try {
        new Intl.DateTimeFormat(undefined, { timeZone: value });
        instanceTimezone = value;
    } catch {
        instanceTimezone = "Asia/Kolkata";
    }
}
export function formatTimestamp(
    value: string | number | null | undefined,
): string {
    if (!value) return "—";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime())
        ? "—"
        : new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeStyle: "long",
              timeZone: instanceTimezone,
          }).format(date);
}
export function formatDate(value: string | number | null | undefined): string {
    if (!value) return "—";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime())
        ? "—"
        : new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeZone: instanceTimezone,
          }).format(date);
}
const mediaRequests = new Map<string, string>();
export function mediaRequestId(path: string, auth = ""): string {
    const key = path + "\0" + auth;
    let id = mediaRequests.get(key);
    if (!id) {
        id = crypto.randomUUID().replaceAll("-", "_");
        mediaRequests.set(key, id);
        if (mediaRequests.size > 256)
            mediaRequests.delete(mediaRequests.keys().next().value!);
    }
    return id;
}
export function fileUrl(path: string, auth = "", download = false): string {
    const search = new URLSearchParams({ path });
    search.set("request_id", mediaRequestId(path, auth));
    if (auth) search.set("auth", auth);
    if (download) search.set("download", "1");
    return "/file?" + search.toString();
}

export interface PreviewDescriptor {
    kind: "text" | "pdf" | "video" | "book" | "unsupported";
    size: number;
    text?: string;
    language?: string;
    truncated?: boolean;
    reason?: string;
    url?: string;
}

export interface FolderTreeRequest {
    destination: string;
    root_name: string;
    folders: string[][];
    client_request_id: string;
}
export interface FolderTreeResponse {
    root: DriveItem;
    folders: Record<string, string>;
}

export interface ManagedClient {
    id: string;
    label: string;
    display_name?: string | null;
    username?: string | null;
    connection_check?: "queued" | "checking" | "waiting" | "passed" | "failed";
    connection_check_error?: string | null;
    connection_checked_at?: number | null;
    connection_retry_at?: number | null;
    identity_refreshing?: boolean;
    identity_retry_at?: number | null;
    kind: "bot" | "user";
    enabled: boolean;
    configured: boolean;
    connected: boolean | null;
    healthy: boolean;
    premium: boolean | null;
    active_leases: number;
    error: string | null;
}
export interface ManagementStats {
    files: number;
    folders: number;
    total_bytes: number;
    trashed_files: number;
    trashed_bytes: number;
    active_jobs: number;
    queued_jobs: number;
    failed_jobs: number;
    staging_budget_bytes: number;
    staging_reserved_bytes: number;
    free_disk_bytes: number | null;
}
export interface ManagementSettings {
    revision: number;
    applied_revision: number;
    restart_required: boolean;
    application_mode: "restart";
    admission_paused: boolean;
    api_id: number;
    api_hash_configured: boolean;
    storage_channel: number;
    storage_channel_error?: string | null;
    clients: ManagedClient[];
    active_clients: Omit<ManagedClient, "enabled" | "configured">[];
    stats: ManagementStats;
    recovery?: DriveProtection | null;
}
export interface ManagedClientUpdate {
    id?: string;
    label: string;
    kind: "bot" | "user";
    enabled: boolean;
    credential?: string;
}
export interface ManagementUpdate {
    expected_revision: number;
    api_id?: number;
    api_hash?: string;
    clients?: ManagedClientUpdate[];
}

export interface PremiumChallenge {
    challenge_id: string;
    stage: "code" | "password";
    expires_in_seconds: number;
}
export type PremiumVerification =
    | PremiumChallenge
    | {
          stage: "complete";
          settings: ManagementSettings;
      };

export interface SearchResults {
    items: DriveItem[];
    query: string;
    has_more: boolean;
    next_offset: number | null;
}

/** A shared unit keeps precise progress counters compact in narrow columns. */
export function formatByteProgress(current: number, total: number): string {
    const sent = formatBytes(current);
    if (!Number.isFinite(total) || total <= 0) return sent;
    const full = formatBytes(total);
    const unit = sent.slice(sent.lastIndexOf(" ") + 1);
    return full.endsWith(" " + unit)
        ? sent.slice(0, sent.lastIndexOf(" ")) + " / " + full
        : sent + " / " + full;
}
export function remainingBytesHint(current: number, total: number): string {
    return Number.isFinite(current) &&
        Number.isFinite(total) &&
        total > 0 &&
        current >= total * 0.95 &&
        current < total
        ? formatBytes(total - current) + " remaining"
        : "";
}
