import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { ApiError, errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
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

export default function PerformanceSettings({
  onSaved,
  category = "cache",
  uploadPreferences,
}: {
  onSaved?: (value: PerformancePreferences) => void;
  category?: "cache" | "transfers" | "regional";
  uploadPreferences?: ReactNode;
} = {}) {
  const [saved, setSaved] = useState<PerformancePreferences | null>(null);
  const [draft, setDraft] = useState<PerformanceDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const value = await performanceApi.settings(signal);
      if (signal?.aborted) return;
      setSaved(value);
      setDraft(performanceDraft(value));
      setConflict(false);
      setNotice("");
    } catch (cause) {
      if (!signal?.aborted) setError(errorMessage(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
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
    if (!saved || !draft) return;
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
      for (const key of Object.keys(refreshed) as (keyof PerformanceDraft)[])
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
      window.dispatchEvent(new Event("tgdrive:performance-settings-changed"));
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
        <label htmlFor={"perf-" + definition.key}>{definition.label}</label>
        <p id={"perf-help-" + definition.key}>
          {definition.help}{" "}
          <span>
            App default: {definition.appDefault}
            {definition.unitLabel ? " " + definition.unitLabel : ""}.
          </span>
        </p>
      </div>
      <input
        id={"perf-" + definition.key}
        type="number"
        min={definition.min}
        max={definition.max}
        step={definition.unit === 1 ? 1 : "any"}
        required
        disabled={busy}
        aria-describedby={"perf-help-" + definition.key}
        value={draft![definition.key]}
        onChange={(event) => {
          setDraft({ ...draft!, [definition.key]: event.target.value });
          setNotice("");
        }}
      />
    </div>
  );
  return (
    <section
      className="settings-section performance-settings"
      aria-labelledby="performance-settings-heading"
    >
      <div className="performance-section-heading">
        <div>
          <h2 id="performance-settings-heading">
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
                cache:
                  "Control server resources and how much media stays ready for reuse.",
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
          href="https://github.com/TechShreyash/TGDrive/blob/dev/docs/performance.md"
          target="_blank"
          rel="noopener noreferrer"
        >
          Guide <ExternalLink size={14} />
        </a>
      </div>
      <div hidden={category !== "transfers"}>{uploadPreferences}</div>
      {loading ? (
        <p className="performance-loading" role="status">
          <Spinner /> Loading performance settings…
        </p>
      ) : saved && draft ? (
        <form noValidate onSubmit={(event) => void save(event)}>
          <fieldset
            className="performance-setting-group"
            hidden={category !== "cache"}
          >
            <legend>Cache storage</legend>
            <p className="settings-help">
              These are maximums, not reserved resources. Available memory and
              disk space can reduce what TGDrive retains. 1 GiB = 1024 MiB; 1
              MiB = 1024 KiB.
            </p>
            {numericPreferences
              .filter((value) => value.key.startsWith("cache_"))
              .map(field)}
          </fieldset>
          <fieldset
            className="performance-setting-group"
            hidden={category !== "cache"}
          >
            <legend>Playback preparation</legend>
            <p className="settings-help">
              Keep useful media ready while active playback and downloads take
              priority.
            </p>
            {numericPreferences
              .filter((value) => value.key === "preload_max_jobs")
              .map(field)}
            <label className="check-field performance-auto-preload">
              <input
                type="checkbox"
                checked={draft.auto_preload}
                disabled={busy}
                onChange={(event) => {
                  setDraft({ ...draft, auto_preload: event.target.checked });
                  setNotice("");
                }}
              />
              <span>
                Preload during playback and downloads
                <small>
                  Warm useful content after sequential bytes are delivered.
                  Metadata and HEAD requests do not start a bulk download.
                  Turning this off leaves manual Prepare available. App default:
                  enabled.
                </small>
              </span>
            </label>
          </fieldset>
          <fieldset
            className="performance-setting-group"
            hidden={category !== "transfers"}
          >
            <legend>Upload splitting</legend>
            {numericPreferences
              .filter((value) => value.key === "upload_split_mib")
              .map(field)}
          </fieldset>
          <fieldset
            className="performance-setting-group"
            hidden={category !== "regional"}
          >
            <legend className="sr-only">Display time</legend>
            <div className="performance-field">
              <div>
                <label htmlFor="performance-timezone">Timezone</label>
                <p id="performance-timezone-help">
                  Dates and share expiry times use this timezone. For IST, use
                  Asia/Kolkata; for another zone, use a name such as
                  Europe/London. Applies to owner and public views. App default:
                  Asia/Kolkata (IST).
                </p>
              </div>
              <input
                id="performance-timezone"
                type="text"
                value={draft.timezone}
                required
                disabled={busy}
                aria-describedby="performance-timezone-help"
                onChange={(event) => {
                  setDraft({ ...draft, timezone: event.target.value });
                  setNotice("");
                }}
              />
            </div>
          </fieldset>
          <details
            className="performance-advanced"
            hidden={category !== "transfers"}
          >
            <summary>Advanced Telegram limits</summary>
            <p>
              These limits share each account’s capacity. Higher values can
              increase cooldowns and compete with streaming.
            </p>
            {numericPreferences.filter((value) => value.advanced).map(field)}
          </details>
          <p className="settings-help performance-save-scope">
            Save and Reset apply only to this page. Changes on other pages stay
            as drafts.
            {category === "transfers" &&
              " Parallel uploads has its own Save Changes action above."}
          </p>
          <p className="settings-help">
            App defaults are built-in reference values. Your deployment may
            start with different environment values; saved settings take
            precedence. Reset Changes restores the last saved values.
          </p>
          <div className="performance-actions">
            <span className="performance-save-state">
              {changed ? "Unsaved changes" : "Settings are up to date"}
            </span>
            <button
              className="primary-button"
              disabled={busy || !changed || conflict}
            >
              {busy ? <Spinner /> : null}
              {busy ? "Saving…" : "Save Performance Settings"}
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={busy}
              onClick={() => {
                if (conflict) {
                  void load();
                  return;
                }
                const baseline = performanceDraft(saved);
                const next = { ...draft };
                for (const key of performanceCategoryKeys(category))
                  Object.assign(next, { [key]: baseline[key] });
                setDraft(next);
                setError("");
                setNotice("");
              }}
            >
              <RefreshCw size={16} />
              {conflict ? "Reload Latest Settings" : "Reset Changes"}
            </button>
          </div>
          {notice && (
            <p className="performance-notice" role="status">
              {notice}
            </p>
          )}
        </form>
      ) : (
        <button className="secondary-button" onClick={() => void load()}>
          Try Again
        </button>
      )}
      <ErrorNotice message={error} />
    </section>
  );
}
