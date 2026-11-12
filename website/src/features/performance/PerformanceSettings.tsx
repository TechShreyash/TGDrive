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
    Minus,
    Plus,
} from "lucide-react";
import {
    Button,
    Fieldset,
    NumberInput,
    NativeSelect,
    TextInput,
    Switch,
    type NumberInputHandlers,
} from "@mantine/core";
import { ApiError, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { useManualRefresh } from "../../shared/useManualRefresh";
import {
    numericPreferences,
    speedLimits,
    resourceLimits,
    performanceApi,
    performanceChanges,
    performanceDraft,
    performanceCategoryDraft,
    performanceCategoryKeys,
    type PerformanceDraft,
    type PerformancePreferences,
    type ResourceLimitDraft,
} from "./performanceApi";
import "./Performance.css";
import DemoModeNotice from "../../shared/DemoModeNotice";
import ThumbnailSettings from "./ThumbnailSettings";
import SettingHelp from "../../shared/SettingHelp";

const RETENTION_UNITS = { minutes: 60, hours: 3600, days: 86400 } as const;

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
    const [retentionUnit, setRetentionUnit] =
        useState<keyof typeof RETENTION_UNITS>("hours");
    const latestLoad = useRef(0);
    const steppers = useRef<
        Partial<Record<string, NumberInputHandlers | undefined>>
    >({});
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
                    JSON.stringify(draft[key]) === JSON.stringify(baseline[key])
                )
                    Object.assign(next, { [key]: refreshed[key] });
            setDraft(next);
            setNotice(
                category === "cache"
                    ? "Cache settings saved."
                    : category === "transfers"
                      ? "Transfer settings saved. Speed limits apply to ongoing transfers; existing files keep their split plan."
                      : "Timezone saved for this installation.",
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
                <SettingHelp label={definition.label}>
                    <p>{definition.help}</p>
                </SettingHelp>
                <p id={"perf-help-" + definition.key}>{definition.help} </p>
            </div>
            <div className="configuration-value number-control">
                <button
                    type="button"
                    className="number-step"
                    tabIndex={-1}
                    aria-label={"Decrease " + definition.label}
                    disabled={
                        demoMode ||
                        busy ||
                        Number(draft![definition.key]) <= definition.min
                    }
                    onClick={() =>
                        steppers.current[definition.key]?.decrement()
                    }
                >
                    <Minus size={16} aria-hidden="true" />
                </button>
                <NumberInput
                    hideControls
                    handlersRef={(handlers) => {
                        steppers.current[definition.key] =
                            handlers ?? undefined;
                    }}
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
                {definition.unitLabel && (
                    <span className="number-unit" aria-hidden="true">
                        {definition.unitLabel}
                    </span>
                )}
                <button
                    type="button"
                    className="number-step"
                    tabIndex={-1}
                    aria-label={"Increase " + definition.label}
                    disabled={
                        demoMode ||
                        busy ||
                        Number(draft![definition.key]) >=
                            (definition.max ?? Infinity)
                    }
                    onClick={() =>
                        steppers.current[definition.key]?.increment()
                    }
                >
                    <Plus size={16} aria-hidden="true" />
                </button>
            </div>
        </div>
    );
    const limitField = (definition: (typeof resourceLimits)[number]) => {
        const value = draft![definition.key];
        const automatic =
            definition.key === "protected_free_bytes"
                ? saved?.auto_free_space_reserve
                : saved?.auto_resource_limits?.[definition.key];
        const automaticLabel =
            typeof automatic === "number"
                ? "Automatic ceiling: " +
                  (automatic / definition.unit).toLocaleString() +
                  " " +
                  definition.unitLabel
                : automatic && typeof automatic === "object"
                  ? "Automatic: the larger of " +
                    (
                        automatic.minimum_bytes / definition.unit
                    ).toLocaleString() +
                    " " +
                    definition.unitLabel +
                    " and " +
                    automatic.fraction * 100 +
                    "% of the reported volume."
                  : "Automatic limit unavailable.";
        const update = (next: ResourceLimitDraft) => {
            setDraft({ ...draft!, [definition.key]: next });
            setNotice("");
        };
        return (
            <div className="performance-field" key={definition.key}>
                <div>
                    <label htmlFor={"limit-mode-" + definition.key}>
                        {definition.label}
                    </label>
                    <SettingHelp label={definition.label}>
                        <p>
                            {definition.key === "stream_memory_limit_bytes"
                                ? "A ceiling for temporary shared stream buffers, separate from retained RAM cache and total process memory. Automatic sizing starts around 32 MiB or lower and grows only with observed foreground waits and verified headroom. Unknown memory does not cause growth. Owned reservations drain safely after a lower ceiling."
                                : definition.key === "protected_free_bytes"
                                  ? "Space left free on the reported media-cache and upload volumes. It is separate from outstanding upload reservations. In Docker or a VM, the visible volume may differ from the physical host."
                                  : definition.key === "staging_limit_bytes"
                                    ? "Uploads, imports and archives reserve this temporary disk space. Lowering the limit preserves existing reservations; new work must fit."
                                    : "Disposable media copies retained for future reads. Automatic selects a resource-based ceiling; custom sets your ceiling; disabled stops retention in this tier. Originals remain in Telegram. Memory cache is part of server RAM."}
                        </p>
                    </SettingHelp>
                    <p id={"limit-help-" + definition.key}>
                        {value.mode === "automatic"
                            ? automaticLabel
                            : value.mode === "disabled"
                              ? "This media retention tier is disabled."
                              : value.mode === "none"
                                ? "No additional TGDrive free-space reserve."
                                : definition.key === "protected_free_bytes"
                                  ? "Minimum free space on media-cache and upload volumes."
                                  : "Maximum " +
                                    definition.unitLabel +
                                    "; automatic tuning may use less."}
                    </p>
                </div>
                <div className="configuration-value resource-limit-controls">
                    <NativeSelect
                        id={"limit-mode-" + definition.key}
                        aria-label={definition.label + " mode"}
                        aria-describedby={"limit-help-" + definition.key}
                        disabled={demoMode || busy}
                        value={value.mode}
                        data={[
                            { value: "automatic", label: "Automatic" },
                            {
                                value: "custom",
                                label:
                                    definition.key === "protected_free_bytes"
                                        ? "Custom reserve"
                                        : "Custom limit",
                            },
                            ...(definition.zeroMode
                                ? [
                                      {
                                          value: definition.zeroMode,
                                          label:
                                              definition.zeroMode === "disabled"
                                                  ? "Disabled"
                                                  : "No extra reserve",
                                      },
                                  ]
                                : []),
                        ]}
                        onChange={(event) =>
                            update({
                                ...value,
                                mode: event.currentTarget
                                    .value as ResourceLimitDraft["mode"],
                            })
                        }
                    />
                    {value.mode === "custom" && (
                        <NumberInput
                            aria-label={
                                definition.label +
                                " (" +
                                definition.unitLabel +
                                ")"
                            }
                            min={definition.min ?? 0}
                            allowNegative={false}
                            step={definition.unitLabel === "MiB" ? 1 : 0.25}
                            suffix={" " + definition.unitLabel}
                            required
                            disabled={demoMode || busy}
                            value={value.value}
                            onChange={(amount) =>
                                update({ ...value, value: String(amount) })
                            }
                        />
                    )}
                </div>
            </div>
        );
    };
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
                                cache: "Automatic tuning stays within your media cache limits.",
                                transfers:
                                    "Control aggregate file speeds, temporary storage and Telegram file splitting.",
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
            <div
                className="settings-card-grid"
                hidden={category !== "transfers"}
            >
                {uploadPreferences}
            </div>
            {loading ? (
                <p className="performance-loading" role="status">
                    <Spinner /> Loading performance settings…
                </p>
            ) : saved && draft ? (
                <form noValidate onSubmit={(event) => void save(event)}>
                    {category === "cache" && (
                        <Fieldset
                            className="performance-setting-group"
                            legend="Media cache limits"
                        >
                            <p className="settings-help">
                                Local file chunks and preloads; the memory limit
                                also includes queued cache writes.
                            </p>
                            {resourceLimits
                                .filter(
                                    (item) =>
                                        item.category === "cache" &&
                                        !item.advanced &&
                                        item.key !==
                                            "stream_memory_limit_bytes",
                                )
                                .map(limitField)}
                            <div className="performance-field">
                                <div>
                                    <label htmlFor="disk-cache-retention">
                                        Disk cache retention
                                    </label>
                                    <SettingHelp label="Disk cache retention">
                                        <p>
                                            Disposable media chunks expire after
                                            inactivity. Reading a chunk
                                            refreshes its expiry. Space pressure
                                            may evict it sooner. Originals in
                                            Telegram and backup archives are
                                            unaffected. This does not configure
                                            RAM retention or total application
                                            memory. Minimum: 1 minute; default:
                                            24 hours (1 day).
                                        </p>
                                    </SettingHelp>
                                    <p id="disk-cache-retention-help">
                                        Expires after inactivity. Space pressure
                                        may evict sooner.
                                    </p>
                                </div>
                                <div className="configuration-value resource-limit-controls">
                                    <NumberInput
                                        id="disk-cache-retention"
                                        aria-label="Disk cache retention"
                                        aria-describedby="disk-cache-retention-help"
                                        min={
                                            60 / RETENTION_UNITS[retentionUnit]
                                        }
                                        max={
                                            Number.MAX_SAFE_INTEGER /
                                            RETENTION_UNITS[retentionUnit]
                                        }
                                        step={1}
                                        allowNegative={false}
                                        required
                                        disabled={demoMode || busy}
                                        value={
                                            draft.cache_ttl_seconds.trim()
                                                ? String(
                                                      (Number(
                                                          draft.cache_ttl_seconds,
                                                      ) *
                                                          3600) /
                                                          RETENTION_UNITS[
                                                              retentionUnit
                                                          ],
                                                  )
                                                : ""
                                        }
                                        onChange={(value) => {
                                            setDraft({
                                                ...draft,
                                                cache_ttl_seconds:
                                                    value === ""
                                                        ? ""
                                                        : String(
                                                              (Number(value) *
                                                                  RETENTION_UNITS[
                                                                      retentionUnit
                                                                  ]) /
                                                                  3600,
                                                          ),
                                            });
                                            setNotice("");
                                        }}
                                    />
                                    <NativeSelect
                                        aria-label="Disk cache retention unit"
                                        value={retentionUnit}
                                        disabled={demoMode || busy}
                                        data={[
                                            {
                                                value: "minutes",
                                                label: "Minutes",
                                            },
                                            { value: "hours", label: "Hours" },
                                            { value: "days", label: "Days" },
                                        ]}
                                        onChange={(event) =>
                                            setRetentionUnit(
                                                event.currentTarget
                                                    .value as keyof typeof RETENTION_UNITS,
                                            )
                                        }
                                    />
                                </div>
                            </div>
                            <details className="performance-detail-note">
                                <summary>Advanced storage protection</summary>
                                {resourceLimits
                                    .filter((item) => item.advanced)
                                    .map(limitField)}
                                <p className="settings-help">
                                    The reserve applies to media-cache and
                                    upload volumes. A VM's reported free space
                                    can differ from its physical host.
                                    Thumbnail/poster caches and recovery
                                    archives are separate from media payload
                                    limits; this memory limit is not total
                                    process RAM.
                                </p>
                            </details>
                        </Fieldset>
                    )}
                    {category === "cache" && (
                        <div className="performance-secondary-settings">
                            <Fieldset
                                className="performance-setting-group"
                                legend="Stream working memory"
                            >
                                <p className="settings-help">
                                    Temporary shared read buffers, separate from
                                    retained media cache.
                                </p>
                                {resourceLimits
                                    .filter(
                                        (item) =>
                                            item.key ===
                                            "stream_memory_limit_bytes",
                                    )
                                    .map(limitField)}
                            </Fieldset>{" "}
                            <Fieldset
                                className="performance-setting-group"
                                data-hue="green"
                                legend={
                                    <span className="configuration-group-title">
                                        <PlayCircle
                                            size={18}
                                            aria-hidden="true"
                                        />
                                        Automatic preloading
                                    </span>
                                }
                            >
                                <p className="settings-help">
                                    Keep useful media ready while active
                                    playback and downloads take priority.
                                </p>
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
                                            auto_preload:
                                                event.currentTarget.checked,
                                        });
                                        setNotice("");
                                    }}
                                />
                            </Fieldset>
                        </div>
                    )}
                    {category === "transfers" && (
                        <>
                            <Fieldset
                                className="performance-setting-group"
                                legend="File transfer speeds"
                            >
                                <p className="settings-help">
                                    One limit per direction across managed file
                                    transfers and all accounts.
                                    <SettingHelp label="File transfer speeds">
                                        <p>
                                            Run a speed test on the hosting
                                            machine. Start around 80% of its
                                            measured upload and download Mbps to
                                            leave room for other traffic. This
                                            is a starting point, not a
                                            guarantee. Limits apply to managed
                                            payloads, including Telegram
                                            transfers, viewer delivery and
                                            native torrents; protocol overhead
                                            and provider retransmissions can
                                            exceed them. Telegram server-side
                                            copies use no host payload. Very low
                                            limits can delay the first complete
                                            Telegram part for minutes. Cancel
                                            transfers from Transfers. Native
                                            torrents reserve an aggregate Python
                                            share even when idle.
                                        </p>
                                    </SettingHelp>
                                </p>
                                {speedLimits.map(({ key, label }) => (
                                    <div
                                        className="performance-field"
                                        key={key}
                                    >
                                        <div>
                                            <label
                                                htmlFor={"speed-mode-" + key}
                                            >
                                                {label}
                                            </label>
                                            <p id={"speed-help-" + key}>
                                                {key ===
                                                "upload_limit_bytes_per_second"
                                                    ? "Files sent to Telegram and delivered to viewers."
                                                    : "Files received from devices, URLs, torrents and Telegram."}
                                            </p>
                                        </div>
                                        <div className="configuration-value resource-limit-controls">
                                            <NativeSelect
                                                id={"speed-mode-" + key}
                                                aria-label={
                                                    label + " limit mode"
                                                }
                                                aria-describedby={
                                                    "speed-help-" + key
                                                }
                                                disabled={demoMode || busy}
                                                value={draft[key].mode}
                                                data={[
                                                    {
                                                        value: "unlimited",
                                                        label: "Unlimited",
                                                    },
                                                    {
                                                        value: "custom",
                                                        label: "Custom limit",
                                                    },
                                                ]}
                                                onChange={(event) => {
                                                    setDraft({
                                                        ...draft,
                                                        [key]: {
                                                            ...draft[key],
                                                            mode: event
                                                                .currentTarget
                                                                .value,
                                                        },
                                                    });
                                                    setNotice("");
                                                }}
                                            />
                                            {draft[key].mode === "custom" && (
                                                <NumberInput
                                                    aria-label={
                                                        label + " limit (Mbps)"
                                                    }
                                                    suffix=" Mbps"
                                                    min={0.008192}
                                                    step={1}
                                                    decimalScale={6}
                                                    allowNegative={false}
                                                    required
                                                    disabled={demoMode || busy}
                                                    value={draft[key].value}
                                                    onChange={(value) => {
                                                        setDraft({
                                                            ...draft,
                                                            [key]: {
                                                                ...draft[key],
                                                                value: String(
                                                                    value,
                                                                ),
                                                            },
                                                        });
                                                        setNotice("");
                                                    }}
                                                />
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </Fieldset>
                            <Fieldset
                                className="performance-setting-group"
                                legend="Temporary upload storage"
                            >
                                {resourceLimits
                                    .filter(
                                        (item) => item.category === "transfers",
                                    )
                                    .map(limitField)}
                                <p className="settings-help">
                                    Uploads, imports and archive work share this
                                    budget. Existing jobs keep their
                                    reservations. New work needs space within
                                    this limit.
                                </p>
                            </Fieldset>
                        </>
                    )}

                    <Fieldset
                        className="performance-setting-group"
                        data-hue="violet"
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
                        data-hue="green"
                        hidden={category !== "regional"}
                        legend={
                            <span className="configuration-group-title">
                                <Globe size={18} aria-hidden="true" />
                                Display time
                            </span>
                        }
                    >
                        <p className="settings-help">
                            Applies to every browser that opens this
                            installation.
                        </p>
                        {category === "regional" && (
                            <TimezonePreview zone={draft.timezone} />
                        )}
                        <div className="performance-field">
                            <div>
                                <label htmlFor="performance-timezone">
                                    Timezone
                                </label>
                                <SettingHelp label="Timezone">
                                    <p>
                                        Use an IANA name such as Asia/Kolkata,
                                        Europe/London or UTC. Dates show their
                                        UTC offset for the date being displayed,
                                        including daylight-saving changes. Share
                                        expiry uses this installation timezone.
                                    </p>
                                </SettingHelp>
                                <p id="performance-timezone-help">
                                    Used for dates and share expiry. Examples:
                                    Asia/Kolkata, Europe/London or UTC.
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
                    <p className="settings-help performance-save-scope">
                        Save applies to this page. Restore defaults loads values
                        for review before saving.
                    </p>
                    <div
                        className="performance-actions"
                        data-dirty={changed || undefined}
                    >
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

function TimezonePreview({ zone }: { zone: string }) {
    const [now, setNow] = useState(() => new Date());
    useEffect(() => {
        const timer = setInterval(() => setNow(new Date()), 1000);
        return () => clearInterval(timer);
    }, []);
    let time = "";
    let date = "";
    let offset = "";
    try {
        const zoneOptions = { timeZone: zone.trim() || "invalid" };
        time = new Intl.DateTimeFormat(undefined, {
            ...zoneOptions,
            timeStyle: "short",
        }).format(now);
        date = new Intl.DateTimeFormat(undefined, {
            ...zoneOptions,
            dateStyle: "full",
        }).format(now);
        offset =
            new Intl.DateTimeFormat("en-US", {
                ...zoneOptions,
                timeZoneName: "longOffset",
            })
                .formatToParts(now)
                .find((part) => part.type === "timeZoneName")
                ?.value.replace(/^GMT/, "UTC") || "";
    } catch {
        time = "";
    }
    return (
        <div className="timezone-preview" data-invalid={!time || undefined}>
            <span className="timezone-preview-label">Current time</span>
            {time ? (
                <>
                    <strong className="timezone-preview-time">{time}</strong>
                    <span className="timezone-preview-date">
                        {date} · {offset}
                    </span>
                </>
            ) : (
                <span className="timezone-preview-date">
                    Enter a valid IANA timezone, such as UTC or Asia/Kolkata.
                </span>
            )}
        </div>
    );
}
