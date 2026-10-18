import { useCallback, useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { api, errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import TransferPanel from "./TransferPanel";
import { mergeHistory, type HistoryFilter } from "./transferHistory";
import type { Job, TransferHistoryPage } from "./types";
import type { Transfer } from "./transfers";

const filters: { key: HistoryFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "queued", label: "Queued" },
  { key: "running", label: "Running" },
  { key: "completed", label: "Completed" },
  { key: "error", label: "Failed" },
  { key: "cancelled", label: "Cancelled" },
];
export default function TransferHistory({
  items,
  onError,
  onLocate,
}: {
  items: Transfer[];
  onLocate: (item: Transfer, folder?: boolean) => void;
  onError: (message: string) => void;
}) {
  const [filter, setFilter] = useState<HistoryFilter>("all");
  const [revision, setRevision] = useState(0);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [page, setPage] = useState<TransferHistoryPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [olderBusy, setOlderBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const loadedOlder = useRef(false);
  const olderController = useRef<AbortController | null>(null);
  const load = useCallback(
    async (signal: AbortSignal, version: number) => {
      try {
        const response = await api.transferHistory(filter, undefined, signal);
        if (signal.aborted || version !== generation.current) return;
        setJobs((previous) => {
          const merged = new Map(previous.map((job) => [job.id, job]));
          for (const job of response.jobs) merged.set(job.id, job);
          return [...merged.values()];
        });
        // Preserve the oldest loaded cursor when new results arrive.
        setPage((previous) => (loadedOlder.current ? previous : response));
        setError("");
      } catch (cause) {
        if (!signal.aborted && version === generation.current)
          setError(errorMessage(cause));
      } finally {
        if (!signal.aborted && version === generation.current)
          setLoading(false);
      }
    },
    [filter],
  );
  useEffect(() => {
    const controller = new AbortController();
    const version = ++generation.current;
    loadedOlder.current = false;
    olderController.current?.abort();
    setOlderBusy(false);
    setJobs([]);
    setPage(null);
    setLoading(true);
    setError("");
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await load(controller.signal, version);
      if (!controller.signal.aborted) timer = setTimeout(poll, 10000);
    };
    void poll();
    return () => {
      controller.abort();
      olderController.current?.abort();
      clearTimeout(timer);
    };
  }, [load, revision]);
  const older = async () => {
    if (
      olderBusy ||
      !page?.has_more ||
      page.next_before === null ||
      !page.next_before_id
    )
      return;
    const controller = new AbortController();
    olderController.current = controller;
    const version = generation.current;
    setOlderBusy(true);
    try {
      const response = await api.transferHistory(
        filter,
        {
          before: page.next_before,
          before_id: page.next_before_id,
        },
        controller.signal,
      );
      if (controller.signal.aborted || generation.current !== version) return;
      setJobs((previous) => {
        const merged = new Map(previous.map((job) => [job.id, job]));
        for (const job of response.jobs)
          if (!merged.has(job.id)) merged.set(job.id, job);
        return [...merged.values()];
      });
      loadedOlder.current = true;
      setPage(response);
      setError("");
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === version)
        setError(errorMessage(cause));
    } finally {
      if (!controller.signal.aborted && generation.current === version)
        setOlderBusy(false);
    }
  };
  const visible = mergeHistory(jobs, items, filter);
  return (
    <section className="transfer-history" aria-label="Transfer History">
      <div
        className="transfer-history-filters transfer-filters"
        role="group"
        aria-label="Filter Transfers"
      >
        {filters.map((entry) => (
          <button
            type="button"
            key={entry.key}
            aria-pressed={filter === entry.key}
            onClick={() => {
              if (entry.key !== filter) setFilter(entry.key);
            }}
          >
            {entry.label}
          </button>
        ))}
      </div>
      {error && (
        <div className="page-load-error">
          <ErrorNotice message={error} />
          <button
            className="secondary-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Try Again
          </button>
        </div>
      )}
      {visible.length ? (
        <TransferPanel
          items={visible}
          onError={onError}
          onLocate={onLocate}
          expandedPage
        />
      ) : loading ? (
        <p className="transfer-history-loading" role="status">
          <Spinner /> Loading history…
        </p>
      ) : !error ? (
        <div className="empty-state">
          <Upload size={36} />
          <h2>
            {filter === "all"
              ? "No Transfers Yet"
              : "No " +
                filters.find((entry) => entry.key === filter)!.label +
                " Transfers"}
          </h2>
          <p>
            {filter === "all"
              ? "Uploads started from My Files appear here, along with their history."
              : "Transfers with this status will appear here."}
          </p>
        </div>
      ) : null}
      {page?.has_more && (
        <div className="transfer-history-more">
          <button
            className="secondary-button"
            disabled={olderBusy}
            onClick={() => void older()}
          >
            {olderBusy ? (
              <>
                <Spinner /> Loading…
              </>
            ) : (
              "Show Older Transfers"
            )}
          </button>
        </div>
      )}
    </section>
  );
}
