import { Button, Paper } from "@mantine/core";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Minus, Plus } from "lucide-react";
import { api, errorMessage, type UploadPreferences } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import {
    useManualRefresh,
    type RefreshNotice,
} from "../../shared/useManualRefresh";
import "./UploadSettings.css";

export default function UploadSettings({
    onNotice,
    demoMode = false,
}: { onNotice?: RefreshNotice; demoMode?: boolean } = {}) {
    const [saved, setSaved] = useState<UploadPreferences | null>(null);
    const [value, setValue] = useState(1);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [loading, setLoading] = useState(true);
    const refreshAction = useManualRefresh(onNotice);
    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        setLoading(true);
        setError("");
        try {
            const preferences = await api.uploadPreferences(signal);
            if (!signal?.aborted) {
                setSaved(preferences);
                setValue(preferences.uploads_per_bot);
            }
            return !signal?.aborted;
        } catch (cause) {
            if (!signal?.aborted) setError(errorMessage(cause));
            if (manual) throw cause;
            return false;
        } finally {
            if (!signal?.aborted) setLoading(false);
        }
    }, []);
    useEffect(() => {
        const controller = new AbortController();
        void load(controller.signal);
        return () => controller.abort();
    }, [load]);
    const save = async (event: FormEvent) => {
        event.preventDefault();
        if (demoMode) return;
        setBusy(true);
        setError("");
        setNotice("");
        try {
            const preferences = await api.saveUploadPreferences(value);
            setSaved(preferences);
            setValue(preferences.uploads_per_bot);
            setNotice("Upload settings saved.");
        } catch (cause) {
            setError(errorMessage(cause));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Paper
            component="section"
            className="settings-section preference-section"
            aria-labelledby="upload-settings-heading"
        >
            <h2 id="upload-settings-heading" className="sr-only">
                Parallel uploads
            </h2>
            <div className="preference-controls">
                {loading ? (
                    <Spinner />
                ) : saved ? (
                    <form onSubmit={(event) => void save(event)}>
                        <div className="upload-limit-setting">
                            <div>
                                <label
                                    id="upload-limit-label"
                                    htmlFor="uploads-per-bot"
                                >
                                    Simultaneous Uploads per Bot
                                </label>
                                <p id="upload-limit-help">
                                    Start with 1; add bots before increasing.
                                </p>
                            </div>
                            <div className="upload-limit-stepper">
                                <Button
                                    type="button"
                                    className="icon-button"
                                    aria-label="Fewer Uploads per Bot"
                                    disabled={
                                        demoMode ||
                                        busy ||
                                        !Number.isInteger(value) ||
                                        value <= 1
                                    }
                                    onClick={() => {
                                        setValue(value - 1);
                                        setNotice("");
                                    }}
                                >
                                    <Minus size={16} />
                                </Button>
                                <input
                                    id="uploads-per-bot"
                                    type="number"
                                    min="1"
                                    max="4"
                                    step="1"
                                    required
                                    aria-describedby="upload-limit-help"
                                    value={value}
                                    disabled={demoMode || busy}
                                    onChange={(event) => {
                                        setValue(event.target.valueAsNumber);
                                        setNotice("");
                                    }}
                                />
                                <Button
                                    type="button"
                                    className="icon-button"
                                    aria-label="More Uploads per Bot"
                                    disabled={
                                        demoMode ||
                                        busy ||
                                        !Number.isInteger(value) ||
                                        value >= 4
                                    }
                                    onClick={() => {
                                        setValue(value + 1);
                                        setNotice("");
                                    }}
                                >
                                    <Plus size={16} />
                                </Button>
                            </div>
                        </div>
                        <p className="settings-help">
                            {saved.connected_bots} connected{" "}
                            {saved.connected_bots === 1 ? "bot" : "bots"} ·{" "}
                            {saved.connected_bots *
                                (Number.isInteger(value)
                                    ? value
                                    : saved.uploads_per_bot)}{" "}
                            upload{" "}
                            {saved.connected_bots *
                                (Number.isInteger(value)
                                    ? value
                                    : saved.uploads_per_bot) ===
                            1
                                ? "slot"
                                : "slots"}
                            .
                        </p>
                        <div className="upload-settings-actions">
                            <Button
                                className="primary-button"
                                type="submit"
                                disabled={
                                    demoMode ||
                                    busy ||
                                    !Number.isInteger(value) ||
                                    value < 1 ||
                                    value > 4 ||
                                    value === saved.uploads_per_bot
                                }
                            >
                                {busy ? "Saving…" : "Save Changes"}
                            </Button>
                            <Button
                                type="button"
                                className="text-button"
                                variant="subtle"
                                disabled={demoMode || busy}
                                onClick={() => {
                                    setValue(1);
                                    setError("");
                                    setNotice(
                                        "Default loaded: 1 upload per bot. Save Changes to apply.",
                                    );
                                }}
                            >
                                Restore upload defaults
                            </Button>
                            <Button
                                type="button"
                                className="text-button"
                                variant="subtle"
                                disabled={
                                    demoMode ||
                                    busy ||
                                    value === saved.uploads_per_bot
                                }
                                hidden={value === saved.uploads_per_bot}
                                onClick={() => {
                                    setValue(saved.uploads_per_bot);
                                    setError("");
                                    setNotice("");
                                }}
                            >
                                Discard upload changes
                            </Button>
                            <span role="status" className="settings-help">
                                {notice}
                            </span>
                        </div>
                    </form>
                ) : (
                    <Button
                        className="secondary-button"
                        disabled={refreshAction.refreshing || loading}
                        onClick={() =>
                            void refreshAction.run(
                                (signal) => load(signal, true),
                                "Upload settings refreshed.",
                            )
                        }
                    >
                        {refreshAction.refreshing && <Spinner />}
                        {refreshAction.refreshing ? "Refreshing…" : "Try Again"}
                    </Button>
                )}
                <ErrorNotice message={error} />
            </div>
        </Paper>
    );
}
