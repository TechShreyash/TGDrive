import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { ApiError, errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import {
  numericPreferences,
  performanceApi,
  performanceChanges,
  performanceDraft,
  type PerformanceDraft,
  type PerformancePreferences,
} from "./performanceApi";
import "./Performance.css";

export default function PerformanceSettings({
  onSaved,
}: { onSaved?: (value: PerformancePreferences) => void } = {}) {
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
  let changed = false;
  if (saved && draft) {
    try {
      changed = Object.keys(performanceChanges(draft, saved)).length > 0;
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
        performanceChanges(draft, saved),
        saved.revision,
      );
      setSaved(value);
      setDraft(performanceDraft(value));
      setNotice("Performance settings saved. New work uses these limits.");
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
        <p id={"perf-help-" + definition.key}>{definition.help}</p>
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
          <h2 id="performance-settings-heading">Performance & Cache</h2>
          <p>
            Control server resources and how much media stays ready for reuse.
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
      {loading ? (
        <p className="performance-loading" role="status">
          <Spinner /> Loading performance settings…
        </p>
      ) : saved && draft ? (
        <form onSubmit={(event) => void save(event)}>
          <div className="performance-field">
            <div>
              <label htmlFor="performance-timezone">Timezone</label>
              <p id="performance-timezone-help">
                Use an IANA name, such as Asia/Kolkata or Europe/London.
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
          {numericPreferences.filter((value) => !value.advanced).map(field)}
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
              Preload during sequential playback
              <small>
                Warm useful content after actual viewing. Metadata and HEAD
                requests do not start a bulk download.
              </small>
            </span>
          </label>
          <details className="performance-advanced">
            <summary>Advanced Telegram limits</summary>
            <p>
              These limits share each account’s capacity. Higher values can
              increase cooldowns and compete with streaming.
            </p>
            {numericPreferences.filter((value) => value.advanced).map(field)}
          </details>
          <div className="performance-actions">
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
              onClick={() => void load()}
            >
              <RefreshCw size={16} />
              {conflict ? "Reload Latest Settings" : "Reset Changes"}
            </button>
          </div>
          <p className="settings-help" role="status">
            {notice}
          </p>
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
