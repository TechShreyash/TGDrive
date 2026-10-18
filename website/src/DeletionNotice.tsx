import { useEffect, useRef, useState } from "react";
import { Check, RefreshCw, Trash2, X } from "lucide-react";
import { Spinner } from "./components";
import { transfers } from "./transfers";
import { errorMessage } from "./api";
import type { Job } from "./types";
import "./DeletionNotice.css";

const failedStates = new Set(["error", "failed", "interrupted"]);
const terminalStates = new Set(["completed", "cancelled", ...failedStates]);

/** Deletion is one operation notice; per-file details are useful only on failure. */
export default function DeletionNotice({ jobs }: { jobs: Job[] }) {
  const active = jobs.filter((job) => !terminalStates.has(job.status));
  const failed = jobs.filter((job) => failedStates.has(job.status));
  const observed = useRef(new Set<string>());
  const [completed, setCompleted] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    for (const job of jobs)
      if (!terminalStates.has(job.status)) observed.current.add(job.id);
    if (active.length || !observed.current.size) return;
    const done = jobs.filter(
      (job) => observed.current.has(job.id) && job.status === "completed",
    ).length;
    observed.current.clear();
    setCompleted(done);
  }, [jobs, active.length]);
  if (!active.length && !failed.length && !completed) return null;
  return (
    <section className="deletion-notice" aria-label="File Deletion">
      <div className="deletion-summary" role="status" aria-live="polite">
        {active.length ? (
          <Spinner />
        ) : failed.length ? (
          <Trash2 size={18} />
        ) : (
          <Check size={18} />
        )}
        <span>
          {active.length
            ? "Deleting " +
              active.length +
              (active.length === 1 ? " item…" : " items…")
            : failed.length
              ? failed.length +
                (failed.length === 1
                  ? " item could not be deleted."
                  : " items could not be deleted.")
              : "Permanently deleted " +
                completed +
                (completed === 1 ? " item." : " items.")}
        </span>
        {!active.length && !failed.length && (
          <button
            className="icon-button"
            aria-label="Dismiss Deletion Result"
            onClick={() => setCompleted(0)}
          >
            <X size={17} />
          </button>
        )}
      </div>
      {failed.length > 0 && (
        <details className="deletion-errors">
          <summary>View Failed Items ({failed.length})</summary>
          <p>These items remain in Trash. You can retry their deletion.</p>
          <ul>
            {failed.map((job) => (
              <li key={job.id}>
                <strong>{job.name || "Item"}</strong>:{" "}
                {job.error || "Telegram has not confirmed deletion."}
              </li>
            ))}
          </ul>
          <button
            className="secondary-button"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setError("");
              void (async () => {
                try {
                  for (const job of failed)
                    await transfers.retryDeletion(job.id);
                } catch (problem) {
                  setError(errorMessage(problem));
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            {busy ? <Spinner /> : <RefreshCw size={16} />} Retry Failed
            deletions
          </button>
          {error && <p role="alert">{error}</p>}
        </details>
      )}
    </section>
  );
}
