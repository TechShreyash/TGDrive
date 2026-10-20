import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Upload } from "lucide-react";
import { api, errorMessage } from "./api";
import { ErrorNotice, Spinner } from "./components";
import TransferPanel from "./TransferPanel";
import { visibleHistory, type HistoryFilter } from "./transferHistory";
import type { Job, TransferHistoryPage } from "./types";
import type { Transfer } from "./transfers";
import "./TransferHistory.css";

const filters: { key: HistoryFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "completed", label: "Successful" },
  { key: "error", label: "Failed" },
  { key: "cancelled", label: "Cancelled" },
];
type Cursor = { before: number; before_id: string } | undefined;
const firstPage = {
  cursor: undefined as Cursor,
  previous: [] as Cursor[],
  number: 1,
};

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
  const [navigation, setNavigation] = useState(firstPage);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [page, setPage] = useState<TransferHistoryPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    const version = ++generation.current;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const response = await api.transferHistory(
          filter === "all" ? "finished" : filter,
          navigation.cursor,
          controller.signal,
        );
        if (controller.signal.aborted || generation.current !== version) return;
        // Only this cursor page is retained. Live uploads come from items above.
        setJobs(response.jobs);
        setPage(response);
        setError("");
      } catch (cause) {
        if (!controller.signal.aborted && generation.current === version)
          setError(errorMessage(cause));
      } finally {
        if (!controller.signal.aborted && generation.current === version) {
          setLoading(false);
          timer = setTimeout(poll, document.hidden ? 30000 : 10000);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [filter, navigation.cursor, revision]);
  const move = (next: typeof navigation) => {
    generation.current++;
    setLoading(true);
    setError("");
    setJobs([]);
    setPage(null);
    setNavigation(next);
  };
  const visible = visibleHistory(jobs, items, filter, navigation.number === 1);
  const controls = (
    <div
      className="transfer-history-filters transfer-filters"
      role="group"
      aria-label="Filter Finished Transfers"
    >
      {filters.map((entry) => (
        <button
          type="button"
          key={entry.key}
          aria-pressed={filter === entry.key}
          onClick={() => {
            if (entry.key !== filter) {
              move(firstPage);
              setFilter(entry.key);
            }
          }}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
  const empty = loading ? (
    <p className="transfer-history-loading" role="status">
      <Spinner /> Loading finished transfers…
    </p>
  ) : !error ? (
    <div className="empty-state">
      <Upload size={32} />
      <h2>
        {filter === "all"
          ? navigation.number === 1
            ? "No Finished Transfers Yet"
            : "No More Finished Transfers"
          : "No " +
            filters.find((entry) => entry.key === filter)!.label +
            " Transfers"}
      </h2>
      <p>
        {filter === "all"
          ? "Successful, failed and cancelled uploads appear here."
          : "Try another filter to see other finished uploads."}
      </p>
    </div>
  ) : null;
  return (
    <section className="transfer-history" aria-label="Transfer History">
      <TransferPanel
        items={visible}
        onError={onError}
        onLocate={onLocate}
        expandedPage
        finishedControls={
          <>
            {controls}
            {error && (
              <div className="page-load-error">
                <ErrorNotice message={error} />
                <button
                  className="secondary-button"
                  onClick={() => {
                    setLoading(true);
                    setRevision((value) => value + 1);
                  }}
                >
                  Try Again
                </button>
              </div>
            )}
          </>
        }
        finishedEmpty={empty}
      />
      <nav
        className="transfer-history-pagination"
        aria-label="Finished transfer pages"
      >
        <span role="status" aria-live="polite">
          Page {navigation.number}
          {loading ? " · Loading…" : ""}
        </span>
        <div>
          {navigation.number > 1 && (
            <button
              className="text-button"
              disabled={loading}
              onClick={() => move(firstPage)}
            >
              Latest
            </button>
          )}
          <button
            className="secondary-button"
            disabled={loading || !navigation.previous.length}
            onClick={() =>
              move({
                cursor: navigation.previous.at(-1),
                previous: navigation.previous.slice(0, -1),
                number: navigation.number - 1,
              })
            }
          >
            <ChevronLeft size={16} /> Previous
          </button>
          <button
            className="secondary-button"
            disabled={
              loading ||
              !page?.has_more ||
              page.next_before === null ||
              !page.next_before_id
            }
            onClick={() => {
              if (
                !page?.has_more ||
                page.next_before === null ||
                !page.next_before_id
              )
                return;
              move({
                cursor: {
                  before: page.next_before,
                  before_id: page.next_before_id,
                },
                previous: [...navigation.previous, navigation.cursor].slice(
                  -128,
                ),
                number: navigation.number + 1,
              });
            }}
          >
            Next <ChevronRight size={16} />
          </button>
        </div>
      </nav>
    </section>
  );
}
