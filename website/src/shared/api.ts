import type {
    Directory,
    FolderTreeRequest,
    FolderTreeResponse,
    Job,
    ManagementSettings,
    ManagementUpdate,
    PublicConfig,
    PremiumChallenge,
    PremiumVerification,
    ServerStatus,
    SearchResults,
    Share,
    UploadSnapshot,
    TransferTaskHistoryPage,
    FolderUploadMetadata,
    TransferLocation,
} from "./types";
export class ApiError extends Error {
    constructor(
        message: string,
        public status = 0,
        public code = "",
        public retryAfter = 0,
    ) {
        super(message);
        this.name = "ApiError";
    }
}
export function notifyDemoFilesReadOnly(code: unknown) {
    if (code === "DEMO_FILES_READ_ONLY")
        window.dispatchEvent(new Event("tgdrive:demo-files-read-only"));
}
let sessionGeneration = 0;
/** Capture before sending: an old response must not expire a newer login. */
export function sessionExpiryHandler() {
    const generation = sessionGeneration;
    return (status: number) => {
        if (status !== 401 || generation !== sessionGeneration) return;
        sessionGeneration++;
        window.dispatchEvent(new Event("tgdrive:auth-required"));
    };
}
export async function request<T>(
    url: string,
    body?: unknown,
    options: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
    const expireSession = sessionExpiryHandler();
    const { timeoutMs = 60000, ...fetchOptions } = options;
    const headers = new Headers(fetchOptions.headers);
    headers.set("Accept", "application/json");
    if (body !== undefined) headers.set("Content-Type", "application/json");
    let response: Response;
    let text: string;
    try {
        response = await fetch(url, {
            credentials: "same-origin",
            ...fetchOptions,
            headers,
            method:
                fetchOptions.method ?? (body === undefined ? "GET" : "POST"),
            body: body === undefined ? fetchOptions.body : JSON.stringify(body),
            signal: fetchOptions.signal
                ? AbortSignal.any([
                      fetchOptions.signal,
                      AbortSignal.timeout(timeoutMs),
                  ])
                : AbortSignal.timeout(timeoutMs),
        });
        if (url !== "/api/checkPassword") expireSession(response.status);
        text = await response.text();
    } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError")
            throw error;
        if (error instanceof DOMException && error.name === "TimeoutError")
            throw new ApiError(
                "TGDrive took too long to respond. Try again.",
                408,
                "REQUEST_TIMEOUT",
            );
        throw new ApiError(
            "Could not reach TGDrive. Check your connection and try again.",
        );
    }
    let data: Record<string, unknown> = {};
    try {
        if (text) data = JSON.parse(text);
    } catch {
        throw new ApiError(
            "The server returned an unreadable response. Try Again.",
            response.status,
        );
    }
    if (
        !response.ok ||
        (typeof data.status === "string" &&
            data.status !== "ok" &&
            data.status !== "cancelled" &&
            !("phase" in data))
    ) {
        const detail =
            typeof data.detail === "string"
                ? data.detail
                : typeof data.error === "string"
                  ? data.error
                  : typeof data.status === "string"
                    ? data.status
                    : "The request could not be completed.";
        if (data.code === "SETTINGS_RESTART_REQUIRED")
            window.dispatchEvent(new Event("tgdrive:restart-required"));
        notifyDemoFilesReadOnly(data.code);
        throw new ApiError(
            detail,
            response.status,
            typeof data.code === "string" ? data.code : detail,
            Math.max(0, Number(response.headers.get("Retry-After")) || 0),
        );
    }
    return data as T;
}
export interface UploadPreferences {
    uploads_per_bot: number;
    connected_bots: number;
    bot_upload_slots: number;
}
export interface BotUploadSettings {
    folder_path: string | null;
    resolved_path: string | null;
    folder_label: string;
    available: boolean;
    enabled: boolean;
    settings_url: string | null;
}
export interface TransferHistoryContext {
    exclude_folders?: string[];
    offset?: number;
}
export const api = {
    botUploadSettings: (signal?: AbortSignal) =>
        request<BotUploadSettings>("/api/v1/settings/bot-mode", undefined, {
            signal,
        }),
    saveBotUploadSettings: (folder_path: string | null) =>
        request<BotUploadSettings>(
            "/api/v1/settings/bot-mode",
            { folder_path, site_url: window.location.origin },
            { method: "PATCH" },
        ),
    starred: (q = "", signal?: AbortSignal) =>
        request<{ data: Directory; auth_home_path?: string }>(
            "/api/v1/starred?" + new URLSearchParams({ q }),
            undefined,
            { signal },
        ),
    star: (path: string, starred: boolean) =>
        request("/api/v1/starred", { path, starred }, { method: "PATCH" }),

    transferLocation: (
        transfer_id: string | undefined,
        destination: string,
        completed: boolean,
    ) =>
        request<TransferLocation>("/api/v1/transfers/location", {
            transfer_id,
            destination,
            completed,
        }),
    uploadPreferences: (signal?: AbortSignal) =>
        request<UploadPreferences>("/api/v1/settings/uploads", undefined, {
            signal,
        }),
    saveUploadPreferences: (uploads_per_bot: number) =>
        request<UploadPreferences>(
            "/api/v1/settings/uploads",
            { uploads_per_bot },
            { method: "PATCH" },
        ),
    config: () =>
        request<PublicConfig>("/api/config", undefined, { cache: "no-store" }),
    session: () =>
        request<{
            authenticated: boolean;
            demo_read_only?: boolean;
            demo_admin?: boolean;
        }>("/api/v1/session", undefined, { cache: "no-store" }),
    login: async (password: string) => {
        for (let attempt = 1; ; attempt++) {
            try {
                const result = await request("/api/checkPassword", {
                    pass: password,
                });
                sessionGeneration++;
                return result;
            } catch (error) {
                if (
                    !(error instanceof ApiError) ||
                    error.status !== 429 ||
                    error.code !== "LOGIN_BUSY" ||
                    attempt >= 3
                )
                    throw error;
                const delay = Math.min(2, Math.max(1, error.retryAfter));
                await new Promise<void>((resolve) =>
                    setTimeout(resolve, delay * 1000),
                );
            }
        }
    },
    logout: async () => {
        const result = await request("/api/logout", {});
        sessionGeneration++;
        return result;
    },
    directory: (path: string, auth = "", signal?: AbortSignal) =>
        request<{ data: Directory; auth_home_path?: string }>(
            "/api/getDirectory",
            { path, auth: auth || null },
            { signal },
        ),
    trash: (query = "", signal?: AbortSignal) =>
        request<{ data: Directory; auth_home_path?: string }>(
            "/api/v1/trash?" + new URLSearchParams({ q: query }),
            undefined,
            { signal },
        ),
    search: (
        query: string,
        offset = 0,
        sort: "name" | "date" | "size" = "name",
        signal?: AbortSignal,
        order?: "asc" | "desc",
    ) => {
        const params = new URLSearchParams({
            q: query,
            offset: String(offset),
            limit: "100",
            sort,
            ...(order ? { order } : {}),
        });
        return request<SearchResults>("/api/v1/search?" + params, undefined, {
            signal,
        });
    },
    folder: (path: string, name: string) =>
        request("/api/createNewFolder", { path, name }),
    prepareFolderTree: (payload: FolderTreeRequest, signal?: AbortSignal) =>
        request<FolderTreeResponse>("/api/v1/folders/tree", payload, {
            signal,
        }),
    rename: (path: string, name: string) =>
        request("/api/renameFileFolder", { path, name }),
    bulk: (
        paths: string[],
        action: "trash" | "restore" | "delete",
        allow_public_exposure = false,
    ) =>
        request<{
            results?: {
                path: string;
                id?: string;
                error?: string;
                status?: string;
            }[];
        }>("/api/v1/nodes/bulk", { paths, action, allow_public_exposure }),
    move: (
        paths: string[],
        destination: string,
        conflict: string,
        allow_public_exposure = false,
    ) =>
        request<{
            results?: {
                path: string;
                id?: string;
                error?: string;
                status?: string;
            }[];
        }>("/api/v1/nodes/move", {
            paths,
            destination,
            conflict,
            allow_public_exposure,
        }),
    shares: () => request<{ shares: Share[] }>("/api/v1/shares"),
    shareLink: (path: string, share_id?: string) =>
        request<{
            auth: string;
            path: string;
            type: "file" | "folder";
            node_id: string;
        }>("/api/v1/shares/link", { path, share_id }),
    share: (
        path: string,
        expires_in_days: number | null,
        label = "",
        expires_at?: string,
    ) =>
        request<{ auth: string; path: string; expires_at: string | null }>(
            "/api/v1/shares",
            { path, expires_in_days, label, expires_at },
        ),
    revoke: (path: string, share_id?: string) =>
        request("/api/v1/shares", { path, share_id }, { method: "DELETE" }),
    status: (signal?: AbortSignal) =>
        request<ServerStatus>("/api/v1/status", undefined, {
            signal,
        }),
    backup: () => request("/api/v1/backup", {}),
    refreshConnections: (signal?: AbortSignal) =>
        request<ManagementSettings>("/api/v1/settings/refresh", {}, { signal }),
    settings: (signal?: AbortSignal) =>
        request<ManagementSettings>("/api/v1/settings", undefined, {
            signal,
        }),
    saveSettings: (value: ManagementUpdate) =>
        request<ManagementSettings>("/api/v1/settings", value, {
            method: "PATCH",
            timeoutMs: 120000,
        }),
    startPremiumLogin: (
        expected_revision: number,
        phone: string,
        label: string,
    ) =>
        request<PremiumChallenge>("/api/v1/settings/premium-login/start", {
            expected_revision,
            phone,
            label,
        }),
    verifyPremiumLogin: (
        challenge_id: string,
        value: { code?: string; password?: string },
    ) =>
        request<PremiumVerification>(
            "/api/v1/settings/premium-login/verify",
            {
                challenge_id,
                ...value,
            },
            { timeoutMs: 120000 },
        ),
    cancelPremiumLogin: (challenge_id: string) =>
        request(
            "/api/v1/settings/premium-login/" +
                encodeURIComponent(challenge_id),
            undefined,
            { method: "DELETE" },
        ),
    jobs: (signal?: AbortSignal) =>
        request<{ jobs: Job[] }>("/api/v1/jobs", undefined, {
            cache: "no-store",
            signal,
        }),
    clearTransferHistory: (excludeFolders: string[] = []) =>
        request<{ cleared: number }>(
            "/api/v1/transfers/history",
            { exclude_folders: excludeFolders },
            { method: "DELETE" },
        ),
    transferTaskHistory: (
        status: string,
        page: number,
        signal?: AbortSignal,
        search = "",
        context: TransferHistoryContext = {},
    ) => {
        return request<TransferTaskHistoryPage>(
            "/api/v1/transfers/history/tasks",
            { status, page, limit: 20, q: search, ...context },
            {
                signal,
            },
        );
    },
    cancelJob: async (id: string) => ({
        data: await request<Job>(
            "/api/v1/jobs/" + encodeURIComponent(id) + "/cancel",
            {},
        ),
    }),
    createUpload: (
        name: string,
        parent_id: string,
        total_size: number,
        folderUpload?: FolderUploadMetadata,
    ) =>
        request<UploadSnapshot>("/api/v1/uploads", {
            name,
            parent_id,
            total_size,
            ...(folderUpload ? { folder_upload: folderUpload } : {}),
        }),
    upload: (id: string) =>
        request<UploadSnapshot>("/api/v1/uploads/" + encodeURIComponent(id)),
    cancelUpload: (id: string) =>
        request("/api/v1/uploads/" + encodeURIComponent(id) + "/cancel", {}),
    completeUpload: (id: string) =>
        request<{ id: string }>(
            "/api/v1/uploads/" + encodeURIComponent(id) + "/complete",
            {},
        ),
    remote: (
        url: string,
        path: string,
        singleThreaded: boolean,
        client_request_id: string,
    ) =>
        request<{ id: string }>("/api/startFileDownloadFromUrl", {
            url,
            path,
            singleThreaded,
            client_request_id,
        }),
    torrent: (url: string, path: string, client_request_id: string) =>
        request<{ id: string }>("/api/startTorrentDownload", {
            url,
            path,
            client_request_id,
        }),
    remoteRequest: (client_request_id: string) =>
        request<{ id: string; data: Job }>("/api/getTransferByRequest", {
            client_request_id,
        }),
    cancelRemoteRequest: (client_request_id: string) =>
        request<{ id: string; data: Job }>("/api/cancelUpload", {
            client_request_id,
        }),
};
export function errorMessage(error: unknown): string {
    return error instanceof Error
        ? error.message
        : "Something went wrong. Please try again.";
}
