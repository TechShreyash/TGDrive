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
  TransferHistoryPage,
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
export async function request<T>(
  url: string,
  body?: unknown,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (body !== undefined) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers,
      method: options.method ?? (body === undefined ? "GET" : "POST"),
      body: body === undefined ? options.body : JSON.stringify(body),
      signal: options.signal ?? AbortSignal.timeout(60000),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      "Could not reach TGDrive. Check your connection and try again.",
    );
  }
  let data: Record<string, unknown> = {};
  const text = await response.text();
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
    if (response.status === 401 && url !== "/api/checkPassword")
      window.dispatchEvent(new Event("tgdrive:auth-required"));
    if (data.code === "SETTINGS_RESTART_REQUIRED")
      window.dispatchEvent(new Event("tgdrive:restart-required"));
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
export const api = {
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
  config: () => request<PublicConfig>("/api/config"),
  session: () => request<{ authenticated: boolean }>("/api/v1/session"),
  login: (password: string) =>
    request("/api/checkPassword", { pass: password }),
  logout: () => request("/api/logout", {}),
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
  ) => {
    const params = new URLSearchParams({
      q: query,
      offset: String(offset),
      limit: "100",
      sort,
    });
    return request<SearchResults>("/api/v1/search?" + params, undefined, {
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(60000)])
        : undefined,
    });
  },
  folder: (path: string, name: string) =>
    request("/api/createNewFolder", { path, name }),
  prepareFolderTree: (payload: FolderTreeRequest, signal?: AbortSignal) =>
    request<FolderTreeResponse>("/api/v1/folders/tree", payload, {
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(60000)])
        : undefined,
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
  shareLink: (path: string) =>
    request<{
      auth: string;
      path: string;
      type: "file" | "folder";
      node_id: string;
    }>("/api/v1/shares/link", { path }),
  share: (path: string, expires_in_days: number | null) =>
    request<{ auth: string; path: string; expires_at: string | null }>(
      "/api/v1/shares",
      { path, expires_in_days },
    ),
  revoke: (path: string) =>
    request("/api/v1/shares", { path }, { method: "DELETE" }),
  status: (signal?: AbortSignal) =>
    request<ServerStatus>("/api/v1/status", undefined, {
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(60000)])
        : undefined,
    }),
  backup: () => request("/api/v1/backup", {}),
  refreshConnections: () =>
    request<ManagementSettings>("/api/v1/settings/refresh", {}),
  settings: (signal?: AbortSignal) =>
    request<ManagementSettings>("/api/v1/settings", undefined, {
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(60000)])
        : undefined,
    }),
  saveSettings: (value: ManagementUpdate) =>
    request<ManagementSettings>("/api/v1/settings", value, {
      method: "PATCH",
      signal: AbortSignal.timeout(120000),
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
      { signal: AbortSignal.timeout(120000) },
    ),
  cancelPremiumLogin: (challenge_id: string) =>
    request(
      "/api/v1/settings/premium-login/" + encodeURIComponent(challenge_id),
      undefined,
      { method: "DELETE" },
    ),
  jobs: () => request<{ jobs: Job[] }>("/api/v1/jobs"),
  transferHistory: (
    status: string,
    cursor?: { before: number; before_id: string },
    signal?: AbortSignal,
    search = "",
  ) => {
    const query = new URLSearchParams({ status, limit: "50" });
    if (search) query.set("q", search);
    if (cursor) {
      query.set("before", String(cursor.before));
      query.set("before_id", cursor.before_id);
    }
    return request<TransferHistoryPage>(
      "/api/v1/transfers/history?" + query,
      undefined,
      { signal },
    );
  },
  job: (id: string) => request<Job>("/api/v1/jobs/" + encodeURIComponent(id)),
  cancelJob: async (id: string) => ({
    data: await request<Job>(
      "/api/v1/jobs/" + encodeURIComponent(id) + "/cancel",
      {},
    ),
  }),
  createUpload: (name: string, parent_id: string, total_size: number) =>
    request<UploadSnapshot>("/api/v1/uploads", { name, parent_id, total_size }),
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
};
export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}
