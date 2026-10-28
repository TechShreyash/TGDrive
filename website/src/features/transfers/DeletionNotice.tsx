import { useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { Spinner } from "../../shared/components";
import { transfers } from "./transfers";
import { errorMessage } from "../../shared/api";
import type { Job } from "../../shared/types";
import "./DeletionNotice.css";

const failedStates = new Set(["error", "failed", "interrupted"]);
const terminalStates = new Set(["completed", "cancelled", ...failedStates]);

/** Deletion is one operation notice; per-file details are useful only on failure. */
export default function DeletionNotice({
    jobs,
    completed,
    progressError,
}: {
    jobs: Job[];
    completed: number;
    progressError: string;
}) {
    const active = jobs.filter((job) => !terminalStates.has(job.status));
    const failed = jobs.filter((job) => failedStates.has(job.status));
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    if (!active.length && !failed.length) return null;
    return (
        <section className="deletion-notice" aria-label="File Deletion">
            <div className="deletion-summary" role="status" aria-live="polite">
                {active.length ? <Spinner /> : <Trash2 size={18} />}
                <span>
                    {active.length
                        ? "Deleting " +
                          active.length +
                          (active.length === 1 ? " item…" : " items…")
                        : failed.length +
                          (failed.length === 1
                              ? " item could not be deleted."
                              : " items could not be deleted.")}
                </span>
            </div>
            {active.length > 0 && (
                <p className="deletion-progress-detail">
                    {completed > 0 ? completed + " completed. " : ""}
                    {active.some((job) => job.phase === "checking_backup")
                        ? "Checking backup protection before deletion. "
                        : active.some(
                                (job) => job.phase === "waiting_for_uploads",
                            )
                          ? "Waiting for uploads into these items to stop. "
                          : ""}
                    Items are hidden while deletion continues in the background.
                    You can close this tab.
                </p>
            )}
            {progressError && <p role="alert">{progressError}</p>}
            {failed.length > 0 && (
                <details className="deletion-errors">
                    <summary>View Failed Items ({failed.length})</summary>
                    <p>
                        These items are back in Trash. You can retry their
                        deletion.
                    </p>
                    <ul>
                        {failed.map((job) => (
                            <li key={job.id}>
                                <strong>{job.name || "Item"}</strong>:{" "}
                                {job.error ||
                                    "TGDrive could not remove this item."}
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
                        {busy ? <Spinner /> : <RefreshCw size={16} />} Retry
                        Failed deletions
                    </button>
                    {error && <p role="alert">{error}</p>}
                </details>
            )}
        </section>
    );
}
