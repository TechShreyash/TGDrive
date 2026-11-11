import { request } from "../../../shared/api";
import { createOrderedProgressWriter } from "../orderedProgressWriter";

const positionPrefix = "tgdrive.video.position.v1:";
export type VideoProgressMode = "server" | "share" | "demo" | "unavailable";
type LocalScope = "share" | "demo";
export const VIDEO_POSITION_CHANGED = "tgdrive:video-position-changed";
export interface VideoPositionChange {
    fileId: string;
    mode: "server" | LocalScope;
    position: VideoPosition;
}
export interface VideoPosition {
    position_seconds: number;
    duration_seconds: number;
    updated_at: number;
}
function localKey(fileId: string, scope: LocalScope) {
    return "tgdrive.video.position.v2:" + scope + ":" + fileId;
}

export function formatVideoTime(seconds: number): string {
    const whole = Math.floor(Math.max(0, seconds));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const remaining = String(whole % 60).padStart(2, "0");
    return hours > 0
        ? `${hours}:${String(minutes).padStart(2, "0")}:${remaining}`
        : `${minutes}:${remaining}`;
}

export function savedVideoResumeTime(time: number, duration: number): number {
    return Number.isFinite(time) &&
        Number.isFinite(duration) &&
        duration > 0 &&
        time > 2 &&
        duration - time > 2
        ? time
        : 0;
}

export function readVideoPosition(
    fileId: string,
    scope: LocalScope,
): number | null {
    try {
        // Legacy reads stay local; owner server history never imports this key.
        const time = Number(
            localStorage.getItem(localKey(fileId, scope)) ??
                localStorage.getItem(positionPrefix + fileId),
        );
        return Number.isFinite(time) && time > 0 ? time : null;
    } catch {
        return null;
    }
}

export function saveVideoPosition(
    fileId: string,
    time: number,
    scope: LocalScope,
    duration?: number,
): void {
    if (!Number.isFinite(time) || time < 0) return;
    try {
        // Zero also masks an older legacy position instead of resurrecting it.
        localStorage.setItem(localKey(fileId, scope), String(time));
        if (
            duration !== undefined &&
            Number.isFinite(duration) &&
            duration > 0 &&
            time <= duration
        ) {
            const rows = localSummaries(scope);
            delete rows[fileId];
            const saved = {
                position_seconds: time,
                duration_seconds: duration,
                updated_at: Date.now() / 1000,
            };
            localStorage.setItem(
                "tgdrive.video.summaries.v1:" + scope,
                JSON.stringify({
                    ...Object.fromEntries(Object.entries(rows).slice(-199)),
                    [fileId]: saved,
                }),
            );
            notifyVideoPosition(fileId, scope, saved);
        }
    } catch {
        // Playback still works when browser storage is unavailable or full.
    }
}

function endpoint(fileId: string) {
    return "/api/v1/files/" + encodeURIComponent(fileId) + "/video-progress";
}
function position(value: unknown): VideoPosition | null {
    if (value === null) return null;
    const row = value as VideoPosition | undefined;
    if (
        !row ||
        !Number.isFinite(row.position_seconds) ||
        row.position_seconds < 0 ||
        !Number.isFinite(row.duration_seconds) ||
        row.duration_seconds <= 0 ||
        row.position_seconds > row.duration_seconds ||
        !Number.isFinite(row.updated_at) ||
        row.updated_at < 0
    )
        throw new Error("The saved video position could not be verified.");
    return {
        position_seconds: row.position_seconds,
        duration_seconds: row.duration_seconds,
        updated_at: row.updated_at,
    };
}

function notifyVideoPosition(
    fileId: string,
    mode: "server" | LocalScope,
    value: unknown,
) {
    const saved = position(value);
    if (saved && typeof window !== "undefined")
        window.dispatchEvent(
            new CustomEvent(VIDEO_POSITION_CHANGED, {
                detail: { fileId, mode, position: saved },
            }),
        );
}

export function videoPositionChange(event: Event): VideoPositionChange | null {
    try {
        if (!(event instanceof CustomEvent)) return null;
        const value = event.detail;
        if (
            !value ||
            typeof value !== "object" ||
            Array.isArray(value) ||
            typeof value.fileId !== "string" ||
            !/^[A-Za-z0-9_-]{1,128}$/.test(value.fileId) ||
            !["server", "share", "demo"].includes(value.mode)
        )
            return null;
        const saved = position(value.position);
        return saved
            ? { fileId: value.fileId, mode: value.mode, position: saved }
            : null;
    } catch {
        return null;
    }
}

function localSummaries(scope: LocalScope): Record<string, VideoPosition> {
    const raw = localStorage.getItem("tgdrive.video.summaries.v1:" + scope);
    if (!raw || raw.length > 128 * 1024) return {};
    try {
        const rows: unknown = JSON.parse(raw);
        if (!rows || typeof rows !== "object" || Array.isArray(rows)) return {};
        return Object.fromEntries(
            Object.entries(rows)
                .filter(([id, value]) => {
                    try {
                        return (
                            /^[A-Za-z0-9_-]{1,128}$/.test(id) &&
                            position(value) !== null
                        );
                    } catch {
                        return false;
                    }
                })
                .sort(([, a], [, b]) => a.updated_at - b.updated_at)
                .slice(-200),
        );
    } catch {
        return {};
    }
}

/** Legacy time-only positions still resume, but cannot establish a fraction. */
export function readLocalVideoPositions(
    fileIds: string[],
    scope: LocalScope,
): Record<string, VideoPosition> {
    try {
        const summaries = localSummaries(scope);
        return Object.fromEntries(
            fileIds.flatMap((id) => {
                const row = Object.hasOwn(summaries, id) ? summaries[id] : null;
                return row &&
                    row.position_seconds === readVideoPosition(id, scope)
                    ? [[id, row]]
                    : [];
            }),
        );
    } catch {
        return {};
    }
}

export async function loadServerVideoPositions(
    fileIds: string[],
    signal: AbortSignal,
): Promise<Record<string, VideoPosition>> {
    const ids = new Set(fileIds);
    if (ids.size > 128)
        throw new Error("Video progress summaries are limited to 128 files.");
    const result = await request<{ progress: unknown }>(
        "/api/v1/video-progress/summaries",
        { file_ids: [...ids] },
        { signal, cache: "no-store", notifySessionExpiry: false },
    );
    if (
        !result.progress ||
        typeof result.progress !== "object" ||
        Array.isArray(result.progress)
    )
        throw new Error("Saved video positions could not be verified.");
    return Object.fromEntries(
        Object.entries(result.progress).map(([id, value]) => {
            const row = position(value);
            if (!ids.has(id) || !row)
                throw new Error("Saved video positions could not be verified.");
            return [id, row];
        }),
    );
}
export async function loadServerVideoPosition(
    fileId: string,
    signal: AbortSignal,
) {
    const result = await request<{ progress: unknown }>(
        endpoint(fileId),
        undefined,
        { signal, cache: "no-store", notifySessionExpiry: false },
    );
    return position(result.progress);
}

/** One playback writer; regular snapshots coalesce, and closing is terminal. */
export function createVideoProgressWriter(
    fileId: string,
    onProblem: (message: string) => void,
) {
    const writer = createOrderedProgressWriter(
        endpoint(fileId),
        position,
        "video",
        onProblem,
        (saved) => notifyVideoPosition(fileId, "server", saved),
    );
    return {
        get closed() {
            return writer.closed;
        },
        start: writer.start,
        save(time: number, duration: number) {
            if (
                !Number.isFinite(time) ||
                time < 0 ||
                !Number.isFinite(duration) ||
                duration <= 0 ||
                time > duration
            )
                return;
            writer.save({ position_seconds: time, duration_seconds: duration });
        },
        close: writer.close,
    };
}
