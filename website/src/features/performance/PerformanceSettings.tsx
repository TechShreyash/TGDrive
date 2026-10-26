import {
    useCallback,
    useEffect,
    useRef,
    useState,
    type FormEvent,
    type ReactNode,
} from "react";
import {
    ExternalLink,
    RefreshCw,
    HardDrive,
    PlayCircle,
    Upload,
    Globe,
} from "lucide-react";
import {
    Button,
    Fieldset,
    NumberInput,
    TextInput,
    Switch,
} from "@mantine/core";
import { ApiError, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { useManualRefresh } from "../../shared/useManualRefresh";
import {
    numericPreferences,
    performanceApi,
    performanceChanges,
    performanceDraft,
    performanceCategoryDraft,
    performanceCategoryKeys,
    type PerformanceDraft,
    type PerformancePreferences,
} from "./performanceApi";
import "./Performance.css";
import DemoModeNotice from "../../shared/DemoModeNotice";
import ThumbnailSettings from "./ThumbnailSettings";

export default function PerformanceSettings({
    onSaved,
    onNotice,
    category = "cache",
    uploadPreferences,
    demoMode = false,
}: {
    onSaved?: (value: PerformancePreferences) => void;
    onNotice?: (message: string, error?: boolean) => void;
    category?: "cache" | "transfers" | "regional";
    uploadPreferences?: ReactNode;
    demoMode?: boolean;
} = {}) {
    const [saved, setSaved] = useState<PerformancePreferences | null>(null);
    const [draft, setDraft] = useState<PerformanceDraft | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [conflict, setConflict] = useState(false);
    const latestLoad = useRef(0);
    const { refreshing, run } = useManualRefresh(
        onNotice ||
            ((message, failed) => {
                if (failed) setError(message);
                else setNotice(message);
            }),
    );
    const load = useCallback(async (signal?: AbortSignal, manual = false) => {
        const request = ++latestLoad.current;
        if (!manual) setLoading(true);
        setError("");
        try {
            const value = await performanceApi.settings(signal);
            if (signal?.aborted || request !== latestLoad.current) return false;
            setSaved(value);
            setDraft(performanceDraft(value));
            setConflict(false);
            setNotice("");
            return true;
        } catch (cause) {
            if (!signal?.aborted && request === latestLoad.current)
                setError(errorMessage(cause));
            if (manual) throw cause;
            return false;
        } finally {
            if (!signal?.aborted && request === latestLoad.current)
                setLoading(false);
        }
    }, []);
    const reloadSettings = () =>
        void run(
            (signal) => load(signal, true),
            "Performance settings refreshed.",
        );
    useEffect(() => {
        const controller = new AbortController();
        void load(controller.signal);
        return () => controller.abort();
    }, [load]);
    useEffect(() => {
        setNotice("");
        setError("");
    }, [category]);
    let changed = false;
    if (saved && draft) {
        try {
            changed =
                Object.keys(
                    performanceChanges(
                        performanceCategoryDraft(draft, saved, category),
                        saved,
                    ),
                ).length > 0;
        } catch {
            changed = true;
        }
    }
    const save = async (event: FormEvent) => {
        event.preventDefault();
        if (demoMode || !saved || !draft) return;
        setBusy(true);
        setError("");
        setNotice("");
        try {
            const value = await performanceApi.save(
                performanceChanges(
                    performanceCategoryDraft(draft, saved, category),
                    saved,
                ),
                saved.revision,
            );
            setSaved(value);
            const refreshed = performanceDraft(value);
            const baseline = performanceDraft(saved);
            const next = { ...draft };
            for (const key of Object.keys(
                refreshed,
            ) as (keyof PerformanceDraft)[])
                if (
                    performanceCategoryKeys(category).includes(key) ||
                    draft[key] === baseline[key]
                )
                    Object.assign(next, { [key]: refreshed[key] });
            setDraft(next);
            setNotice(
                "Performance settings saved for this section. New work uses these limits.",
            );
            setConflict(false);
            onSaved?.(value);
            window.dispatchEvent(
                new Event("tgdrive:performance-settings-changed"),
            );
        } catch (cause) {
            setError(errorMessage(cause));
            setConflict(cause instanceof ApiError && cause.status === 409);
        } finally {
            setBusy(false);
        }
    };
    const field = (definition: (typeof numericPreferences)[number]) => (
        <div className="performance-field" key={definition.key}>
            <div>
                <label htmlFor={"perf-" + definition.key}>
                    {definition.label}
                </label>
                <p id={"perf-help-" + definition.key}>{definition.help} </p>
            </div>
            <div className="configuration-value">
                <NumberInput
                    id={"perf-" + definition.key}
                    min={definition.min}
                    max={definition.max}
                    step={definition.unit === 1 ? 1 : 0.25}
                    allowDecimal={definition.unit !== 1}
                    allowNegative={false}
                    aria-label={definition.label}
                    required
                    disabled={demoMode || busy}
                    aria-describedby={"perf-help-" + definition.key}
                    value={draft![definition.key]}
                    onChange={(value) => {
                        setDraft({
                            ...draft!,
                            [definition.key]: String(value),
                        });
                        setNotice("");
                    }}
                />
            </div>
        </div>
    );
    return (
        <section
            className="settings-section performance-settings"
            aria-labelledby="performance-settings-heading"
        >
            {demoMode && <DemoModeNotice />}
            <div className="performance-section-heading">
                <div>
                    <h2 id="performance-settings-heading" className="sr-only">
                        {
                            {
                                cache: "Cache storage and playback preparation",
                                transfers: "Telegram transfer policy",
                                regional: "Instance timezone",
                            }[category]
                        }
                    </h2>
                    <p>
                        {
                            {
                                cache: "Control server resources and how much media stays ready for reuse.",
                                transfers:
                                    "Control upload splitting and the request capacity shared by Telegram clients.",
                                regional:
                                    "Choose the timezone used for dates and share expiry across this installation.",
                            }[category]
                        }
                    </p>
                </div>
                <a
                    className="inline-link"
                    href={
                        saved?.documentation_url ||
                        "https://github.com/TechShreyash/TGDrive/blob/main/docs/configuration.md"
                    }
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    Configuration guide <ExternalLink size={14} />
                </a>
            </div>
            <div hidden={category !== "transfers"}>{uploadPreferences}</div>
            {loading ? (
                <p className="performance-loading" role="status">
                    <Spinner /> Loading performance settings…
                </p>
            ) : saved && draft ? (
                <form noValidate onSubmit={(event) => void save(event)}>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "cache"}
                        legend={
                            <span className="configuration-group-title">
                                <HardDrive size={18} aria-hidden="true" />
                                Cache storage
                            </span>
                        }
                    >
                        <p className="settings-help">
                            Budgets are shared by playback, downloads and
                            preloads. They are filled only when needed.
                        </p>
                        {numericPreferences
                            .filter((value) => value.key.startsWith("cache_"))
                            .map(field)}
                    </Fieldset>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "cache"}
                        legend={
                            <span className="configuration-group-title">
                                <PlayCircle size={18} aria-hidden="true" />
                                Automatic preloading
                            </span>
                        }
                    >
                        <p className="settings-help">
                            Keep useful media ready while active playback and
                            downloads take priority.
                        </p>
                        {numericPreferences
                            .filter((value) => value.key === "preload_max_jobs")
                            .map(field)}
                        <Switch
                            className="configuration-auto-preload"
                            label="Preload during playback"
                            description="Adapts extra server caching to media playback. Downloads fetch their requested ranges. Requested chunks and adaptive read-ahead still work when off; manual whole-file preloads stay available."
                            checked={draft.auto_preload}
                            onLabel="ON"
                            offLabel="OFF"
                            disabled={demoMode || busy}
                            onChange={(event) => {
                                setDraft({
                                    ...draft,
                                    auto_preload: event.currentTarget.checked,
                                });
                                setNotice("");
                            }}
                        />
                    </Fieldset>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "transfers"}
                        legend={
                            <span className="configuration-group-title">
                                <Upload size={18} aria-hidden="true" />
                                Upload splitting
                            </span>
                        }
                    >
                        {numericPreferences
                            .filter((value) => value.key === "upload_split_mib")
                            .map(field)}
                    </Fieldset>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "transfers"}
                        legend={
                            <span className="configuration-group-title">
                                <Globe size={18} aria-hidden="true" />
                                URL downloads
                            </span>
                        }
                    >
                        {numericPreferences
                            .filter(
                                (value) =>
                                    value.key === "remote_download_concurrency",
                            )
                            .map(field)}
                    </Fieldset>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "transfers"}
                        legend={
                            <span className="configuration-group-title">
                                <HardDrive size={18} aria-hidden="true" />
                                Upload storage
                            </span>
                        }
                    >
                        {numericPreferences
                            .filter((value) =>
                                [
                                    "max_file_size",
                                    "staging_budget_bytes",
                                    "min_free_bytes",
                                ].includes(value.key),
                            )
                            .map(field)}
                    </Fieldset>
                    <Fieldset
                        className="performance-setting-group"
                        hidden={category !== "regional"}
                        legend={
                            <span className="configuration-group-title">
                                <Globe size={18} aria-hidden="true" />
                                Display time
                            </span>
                        }
                    >
                        <div className="performance-field">
                            <div>
                                <label htmlFor="performance-timezone">
                                    Timezone
                                </label>
                                <p id="performance-timezone-help">
                                    Used for dates and share expiry. Examples:
                                    Asia/Kolkata (IST), Europe/London or UTC.
                                </p>
                            </div>
                            <TextInput
                                id="performance-timezone"
                                value={draft.timezone}
                                required
                                disabled={demoMode || busy}
                                aria-describedby="performance-timezone-help"
                                onChange={(event) => {
                                    setDraft({
                                        ...draft,
                                        timezone: event.target.value,
                                    });
                                    setNotice("");
                                }}
                            />
                        </div>
                    </Fieldset>
                    <details
                        className="performance-advanced"
                        hidden={category !== "transfers"}
                    >
                        <summary>Advanced Telegram limits</summary>
                        <p>
                            These limits share each account’s capacity. Higher
                            values can increase cooldowns and compete with
                            streaming.
                        </p>
                        {numericPreferences
                            .filter((value) => value.advanced)
                            .map(field)}
                    </details>
                    <p className="settings-help performance-save-scope">
                        Save applies to this page. Restore defaults loads values
                        for review before saving.
                        {category === "transfers" &&
                            " Parallel uploads has its own Save Changes action above."}
                    </p>
                    <div className="performance-actions">
                        <span className="performance-save-state">
                            {changed
                                ? "Unsaved changes"
                                : "Settings are up to date"}
                        </span>
                        <Button
                            className="primary-button"
                            type="submit"
                            aria-label="Save Performance Settings"
                            disabled={demoMode || busy || !changed || conflict}
                        >
                            {busy ? <Spinner /> : null}
                            {busy ? "Saving…" : "Save changes"}
                        </Button>
                        <Button
                            type="button"
                            className="secondary-button"
                            variant="default"
                            disabled={demoMode || busy || refreshing}
                            onClick={() => {
                                if (conflict) {
                                    reloadSettings();
                                    return;
                                }
                                const baseline = performanceDraft(saved);
                                const next = { ...draft };
                                for (const key of performanceCategoryKeys(
                                    category,
                                ))
                                    Object.assign(next, {
                                        [key]: baseline[key],
                                    });
                                setDraft(next);
                                setError("");
                                setNotice("");
                            }}
                        >
                            {refreshing ? <Spinner /> : <RefreshCw size={16} />}
                            {refreshing
                                ? "Refreshing…"
                                : conflict
                                  ? "Reload Latest Settings"
                                  : "Discard changes"}
                        </Button>
                        <Button
                            type="button"
                            className="text-button"
                            variant="subtle"
                            disabled={
                                demoMode || busy || conflict || !saved.defaults
                            }
                            onClick={() => {
                                if (!saved.defaults) return;
                                const defaults = performanceDraft(
                                    saved.defaults,
                                );
                                const next = { ...draft };
                                for (const key of performanceCategoryKeys(
                                    category,
                                ))
                                    Object.assign(next, {
                                        [key]: defaults[key],
                                    });
                                setDraft(next);
                                setError("");
                                setNotice(
                                    "Default values loaded for this page. Review and save to apply.",
                                );
                            }}
                        >
                            Restore defaults
                        </Button>
                    </div>
                    {notice && (
                        <p className="performance-notice" role="status">
                            {notice}
                        </p>
                    )}
                </form>
            ) : (
                <Button
                    className="secondary-button"
                    disabled={refreshing}
                    aria-busy={refreshing}
                    onClick={reloadSettings}
                >
                    {refreshing && <Spinner />}
                    {refreshing ? "Refreshing…" : "Try Again"}
                </Button>
            )}
            <ErrorNotice message={error} />
            {category === "cache" && <ThumbnailSettings demoMode={demoMode} />}
        </section>
    );
}
