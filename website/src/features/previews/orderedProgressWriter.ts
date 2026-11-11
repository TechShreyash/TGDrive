import { ApiError, request } from "../../shared/api";

/** Ordered saves shared by the video and PDF readers; closing is terminal. */
export function createOrderedProgressWriter<Snapshot extends object>(
    url: string,
    validate: (value: unknown) => unknown,
    kind: "video" | "pdf",
    onProblem: (message: string) => void,
    onSaved?: (value: unknown) => void,
) {
    const prefix = kind.toUpperCase() + "_PROGRESS_";
    const subject = kind === "video" ? "video" : "PDF";
    const sessionName = kind === "video" ? "playback" : "reading";
    const activity = kind === "video" ? "Playback" : "Reading";
    let session: { session_id: string; save_interval_seconds: number } | null =
        null;
    let beginning: Promise<void> | null = null;
    let writing: Promise<void> | null = null;
    let latest: Snapshot | null = null;
    let latestKey: string | null = null;
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
            [prefix + "SESSION_CHANGED", prefix + "SEQUENCE_STALE"].includes(
                cause.code,
            );
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
                      cause.code === prefix + "SEQUENCE_STALE"
                        ? `A newer ${subject} position is already saved. Reopen this ${subject} to save from here.`
                        : `Another ${sessionName} session has newer ${subject} progress. Reopen this ${subject} to save from here.`
                    : `${kind === "video" ? "Video" : "PDF"} progress could not be saved to the server. ${activity} still works; reopen this ${subject} to reload saved progress.`,
            );
    };
    const put = async (final: boolean) => {
        if (!session || !latest || stopped) return;
        const result = await request<{ progress: unknown }>(
            url,
            {
                session_id: session.session_id,
                sequence: ++sequence,
                ...latest,
                final,
            },
            { method: "PUT", keepalive: final, notifySessionExpiry: false },
        );
        const saved = validate(result.progress);
        onSaved?.(saved);
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
            url + "/session",
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
                        `The ${subject} progress session could not be verified.`,
                    );
                validate(result.progress);
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
                    cause.code === prefix + "RATE_LIMITED"
                ) {
                    beginning = null;
                    onProblem(
                        kind === "video"
                            ? "Waiting to save your video position…"
                            : "Waiting to save your PDF page…",
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
        save(value: Snapshot) {
            if (closed) return;
            const key = JSON.stringify(value);
            if (key === latestKey) return;
            latest = value;
            latestKey = key;
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
