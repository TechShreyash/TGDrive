import { useEffect, useRef, useState } from "react";
import { Button, Fieldset } from "@mantine/core";
import { RefreshCw } from "lucide-react";
import { errorMessage, request } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import { formatBytes } from "../../shared/types";
import "./ThumbnailSettings.css";

export type ThumbnailRebuild = {
    status:
        | "idle"
        | "running"
        | "completed"
        | "cancelled"
        | "interrupted"
        | "failed";
    phase: string;
    total: number;
    processed: number;
    generated: number;
    skipped: number;
    failed: number;
    source_bytes: number;
    persistence_warning?: string;
    current_bytes?: number;
    current_name?: string;
    recent_issues?: { name: string; reason: string }[];
};
export function rebuildPercent(state: ThumbnailRebuild): number | undefined {
    if (state.phase === "Finding files") return undefined;
    if (!state.total) return state.status === "completed" ? 100 : undefined;
    return Math.min(
        100,
        Math.max(0, Math.floor((state.processed / state.total) * 100)),
    );
}
const endpoint = "/api/v1/settings/thumbnails/rebuild";
export default function ThumbnailSettings({
    demoMode = false,
}: {
    demoMode?: boolean;
}) {
    const [state, setState] = useState<ThumbnailRebuild | null>(null);
    const [confirm, setConfirm] = useState(false);
    const [busy, setBusy] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const [loadError, setLoadError] = useState("");
    const [error, setError] = useState("");
    const action = useRef(false);
    const mounted = useRef(true);
    const generation = useRef(0);
    useEffect(() => {
        mounted.current = true;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            let delay = 2000;
            if (!action.current) {
                const id = ++generation.current;
                try {
                    const value = await request<ThumbnailRebuild>(
                        endpoint,
                        undefined,
                        {
                            signal: controller.signal,
                            timeoutMs: 15000,
                        },
                    );
                    if (
                        !controller.signal.aborted &&
                        id === generation.current
                    ) {
                        setState(value);
                        setLoadError("");
                    }
                    delay = value.status === "running" ? 2000 : 10000;
                } catch (cause) {
                    if (!controller.signal.aborted && id === generation.current)
                        setLoadError(errorMessage(cause));
                }
            }
            if (!controller.signal.aborted) timer = setTimeout(poll, delay);
        };
        void poll();
        return () => {
            mounted.current = false;
            controller.abort();
            clearTimeout(timer);
            ++generation.current;
        };
    }, [refresh]);
    const change = async (cancel = false) => {
        if (demoMode || action.current) return;
        action.current = true;
        ++generation.current;
        setBusy(true);
        setError("");
        try {
            const value = await request<ThumbnailRebuild>(
                endpoint + (cancel ? "/cancel" : ""),
                cancel ? {} : { confirmed: true },
            );
            if (mounted.current) {
                setState(value);
                setConfirm(false);
                setRefresh((value) => value + 1);
            }
        } catch (cause) {
            if (mounted.current) setError(errorMessage(cause));
        } finally {
            action.current = false;
            if (mounted.current) setBusy(false);
        }
    };
    const running = state?.status === "running";
    const percent = state ? rebuildPercent(state) : undefined;
    return (
        <Fieldset
            className="performance-setting-group thumbnail-settings"
            legend="Thumbnails"
        >
            <p className="settings-help">
                New uploads generate thumbnails automatically. Rebuild covers
                for files already stored, including old or missing covers.
                Originals stay unchanged.
            </p>
            {state && state.status !== "idle" && (
                <div className="thumbnail-rebuild-status">
                    <p role="status">
                        {running && <Spinner />} {state.phase}
                    </p>
                    <progress
                        aria-label="Thumbnail rebuild progress"
                        max={100}
                        value={percent}
                    />
                    <p>
                        {state.processed} / {state.total} files checked
                        {percent !== undefined ? " · " + percent + "%" : ""}
                    </p>
                    <p>
                        {state.generated} regenerated · {state.skipped} skipped
                        · {state.failed} failed
                    </p>
                    {running && state.current_name && (
                        <p className="thumbnail-current-file">
                            {state.current_name} ·{" "}
                            {formatBytes(state.current_bytes)} read
                        </p>
                    )}
                    <p className="settings-help">
                        {formatBytes(state.source_bytes)} source data read,
                        including cached data. Existing covers remain when
                        generation fails.
                    </p>
                    {(state.recent_issues?.length || 0) > 0 && (
                        <details>
                            <summary>Recent skipped or failed files</summary>
                            <ul>
                                {state.recent_issues?.map((issue, i) => (
                                    <li key={i}>
                                        {issue.name}: {issue.reason}
                                    </li>
                                ))}
                            </ul>
                        </details>
                    )}
                </div>
            )}
            {!confirm && <ErrorNotice message={error || loadError} />}
            <ErrorNotice message={state?.persistence_warning || ""} />
            <Button
                type="button"
                variant="default"
                disabled={demoMode || busy || !state}
                onClick={() => (running ? void change(true) : setConfirm(true))}
            >
                {busy ? <Spinner /> : <RefreshCw size={16} />}{" "}
                {running ? "Cancel rebuild" : "Regenerate all thumbnails"}
            </Button>
            {confirm && (
                <Modal
                    title="Regenerate all thumbnails?"
                    onClose={() => {
                        if (!busy) setConfirm(false);
                    }}
                    initialFocus="[data-cancel-rebuild]"
                >
                    <p>
                        This can take a long time and use substantial bandwidth
                        when many files are stored. Cached data is reused where
                        possible; some formats, especially PDFs, need larger
                        reads.
                    </p>
                    <p>
                        The task runs on the server after you close this page.
                        It processes one file at a time, yields to active
                        transfers and playback, and can be cancelled here. Some
                        unsupported or oversized files will keep their existing
                        cover or icon.
                    </p>
                    <ErrorNotice message={error} />
                    <div className="thumbnail-rebuild-actions">
                        <Button
                            type="button"
                            variant="default"
                            data-cancel-rebuild
                            disabled={busy}
                            onClick={() => setConfirm(false)}
                        >
                            Cancel
                        </Button>
                        <Button
                            type="button"
                            disabled={busy}
                            onClick={() => void change()}
                        >
                            {busy && <Spinner />} Start background rebuild
                        </Button>
                    </div>
                </Modal>
            )}
        </Fieldset>
    );
}
