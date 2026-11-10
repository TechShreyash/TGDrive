import { ApiError, request } from "../../../shared/api";

const positionPrefix = "tgdrive.video.position.v1:";
export type VideoProgressMode = "server" | "share" | "demo" | "unavailable";
type LocalScope = "share" | "demo";
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
): void {
    if (!Number.isFinite(time) || time < 0) return;
    try {
        // Zero also masks an older legacy position instead of resurrecting it.
        localStorage.setItem(localKey(fileId, scope), String(time));
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
    return row;
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
    let session: { session_id: string; save_interval_seconds: number } | null =
        null;
    let beginning: Promise<void> | null = null;
    let writing: Promise<void> | null = null;
    let latest: { position_seconds: number; duration_seconds: number } | null =
        null;
    let sequence = 0,
        beginAttempts = 0;
    let nextSaveAt = 0,
        closed = false,
        stopped = false,
        dirty = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const problem = (cause: unknown) => {
        const conflict =
            cause instanceof ApiError &&
            [
                "VIDEO_PROGRESS_SESSION_CHANGED",
                "VIDEO_PROGRESS_SEQUENCE_STALE",
            ].includes(cause.code);
        if (
            cause instanceof ApiError &&
            [401, 403, 404, 409, 410].includes(cause.status)
        )
            stopped = true;
        if (cause instanceof ApiError && cause.status === 429)
            nextSaveAt = Math.max(
                nextSaveAt,
                performance.now() + Math.max(1, cause.retryAfter) * 1000,
            );
        if (!closed)
            onProblem(
                conflict
                    ? cause instanceof ApiError &&
                      cause.code === "VIDEO_PROGRESS_SEQUENCE_STALE"
                        ? "A newer video position is already saved. Reopen this video to save from here."
                        : "Another playback session has newer video progress. Reopen this video to save from here."
                    : "Video progress could not be saved to the server. Playback still works; reopen this video to reload saved progress.",
            );
    };
    const put = async (final: boolean) => {
        if (!session || !latest || stopped) return;
        const result = await request<{ progress: unknown }>(
            endpoint(fileId),
            {
                session_id: session.session_id,
                sequence: ++sequence,
                ...latest,
                final,
            },
            { method: "PUT", keepalive: final, notifySessionExpiry: false },
        );
        position(result.progress);
        if (!closed) onProblem("");
    };
    const schedule = () => {
        if (
            closed ||
            stopped ||
            !session ||
            !latest ||
            writing ||
            timer ||
            !dirty
        )
            return;
        const remaining = nextSaveAt - performance.now();
        if (remaining > 0) {
            timer = setTimeout(() => {
                timer = undefined;
                schedule();
            }, remaining);
            return;
        }
        dirty = false;
        writing = put(false)
            .catch(problem)
            .finally(() => {
                writing = null;
                nextSaveAt = Math.max(
                    nextSaveAt,
                    performance.now() + session!.save_interval_seconds * 1000,
                );
                schedule();
            });
    };
    const begin = () => {
        if (beginning || timer || closed || stopped || beginAttempts >= 2)
            return;
        beginAttempts++;
        beginning = request<{
            session_id: string;
            progress: unknown;
            save_interval_seconds: number;
        }>(
            endpoint(fileId) + "/session",
            {},
            { method: "POST", keepalive: true, notifySessionExpiry: false },
        )
            .then((result) => {
                if (
                    !/^[A-Za-z0-9_-]{32}$/.test(result.session_id) ||
                    !Number.isFinite(result.save_interval_seconds) ||
                    result.save_interval_seconds <= 0
                )
                    throw new Error(
                        "The video progress session could not be verified.",
                    );
                position(result.progress);
                session = result;
                schedule();
            })
            .catch((cause) => {
                // A confirmed rate rejection created no writer; retry it once after admission opens.
                if (
                    !closed &&
                    beginAttempts < 2 &&
                    cause instanceof ApiError &&
                    cause.status === 429 &&
                    cause.code === "VIDEO_PROGRESS_RATE_LIMITED"
                ) {
                    beginning = null;
                    onProblem(
                        "Video progress is waiting for server admission…",
                    );
                    timer = setTimeout(
                        () => {
                            timer = undefined;
                            begin();
                        },
                        Math.max(1, cause.retryAfter) * 1000,
                    );
                } else problem(cause);
            });
    };
    return {
        get closed() {
            return closed;
        },
        start() {
            begin();
        },
        save(time: number, duration: number) {
            if (
                closed ||
                !Number.isFinite(time) ||
                time < 0 ||
                !Number.isFinite(duration) ||
                duration <= 0 ||
                time > duration
            )
                return;
            if (
                latest?.position_seconds === time &&
                latest.duration_seconds === duration
            )
                return;
            latest = { position_seconds: time, duration_seconds: duration };
            dirty = true;
            schedule();
        },
        close() {
            if (closed) return;
            closed = true;
            clearTimeout(timer);
            void (async () => {
                await beginning;
                await writing;
                await put(true);
            })().catch(problem);
        },
    };
}
