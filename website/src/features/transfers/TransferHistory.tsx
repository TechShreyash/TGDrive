import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Trash2, Upload } from "lucide-react";
import { errorMessage } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import TransferPanel from "./TransferPanel";
import {
    loadTaskHistoryPage,
    liveHistoryRows,
    matchHistoryTasks,
    visibleHistory,
    type HistoryFilter,
} from "./transferHistory";
import type { Job, TransferTaskHistoryPage } from "../../shared/types";
import { transfers, type Transfer } from "./transfers";
import {
    useManualRefresh,
    type RefreshNotice,
} from "../../shared/useManualRefresh";
import "./TransferHistory.css";

const filters: { key: HistoryFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "completed", label: "Successful" },
    { key: "error", label: "Failed" },
    { key: "cancelled", label: "Cancelled" },
];
export default function TransferHistory({
    items,
    onError,
    onLocate,
    onNotice,
    query = "",
}: {
    items: Transfer[];
    onLocate: (item: Transfer, folder?: boolean) => void;
    onError: (message: string) => void;
    onNotice?: RefreshNotice;
    query?: string;
}) {
    const [filter, setFilter] = useState<HistoryFilter>("all");
    const [pageNumber, setPageNumber] = useState(1);
    const [jobs, setJobs] = useState<Job[]>([]);
    const [localItems, setLocalItems] = useState<Transfer[]>([]);
    const [page, setPage] = useState<TransferTaskHistoryPage | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [confirmClear, setConfirmClear] = useState(false);
    const [clearing, setClearing] = useState(false);
    const [clearError, setClearError] = useState("");
    const [historyVersion, setHistoryVersion] = useState(0);
    const clearingRef = useRef(false);
    const generation = useRef(0);
    const currentItems = useRef(items);
    currentItems.current = items;
    const manual = useManualRefresh(onNotice);
    const context = JSON.stringify([filter, pageNumber, query]);
    const manualContext = useRef<string | null>(null);
    const load = useCallback(
        async (signal: AbortSignal, explicit = false) => {
            if (clearingRef.current) return false;
            const version = ++generation.current;
            let redirected = false;
            try {
                const response = await loadTaskHistoryPage(
                    currentItems.current,
                    filter,
                    pageNumber,
                    signal,
                    query,
                    (rows) => {
                        if (!signal.aborted && generation.current === version)
                            setLocalItems(rows);
                    },
                );
                if (signal.aborted || generation.current !== version)
                    return false;
                if (response.page !== pageNumber) {
                    redirected = true;
                    setJobs([]);
                    setLocalItems([]);
                    setPage(response);
                    setLoading(true);
                    setPageNumber(response.page);
                    return true;
                }
                setJobs(response.jobs);
                setLocalItems(response.localItems);
                setPage(response);
                setError("");
                return true;
            } catch (cause) {
                if (signal.aborted || generation.current !== version)
                    return false;
                setJobs([]);
                setPage(null);
                if (pageNumber > 1) setPageNumber(1);
                setError(errorMessage(cause));
                if (explicit) throw cause;
                return false;
            } finally {
                if (
                    !redirected &&
                    !signal.aborted &&
                    generation.current === version
                )
                    setLoading(false);
            }
        },
        [filter, pageNumber, query],
    );
    useEffect(() => {
        move(1, true);
    }, [query]);
    useEffect(() => {
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            if (manualContext.current !== context)
                await load(controller.signal);
            if (!controller.signal.aborted)
                timer = setTimeout(poll, document.hidden ? 30000 : 10000);
        };
        void poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [load, context, historyVersion]);
    const clearHistory = async () => {
        if (clearingRef.current) return;
        clearingRef.current = true;
        setClearing(true);
        setClearError("");
        generation.current++;
        try {
            await transfers.clearHistory();
            generation.current++;
            setJobs([]);
            setLocalItems([]);
            setPage(null);
            setPageNumber(1);
            setLoading(true);
            setError("");
            setConfirmClear(false);
            onNotice?.("Transfer history cleared.");
        } catch (cause) {
            setClearError(errorMessage(cause));
        } finally {
            clearingRef.current = false;
            setClearing(false);
            setHistoryVersion((version) => version + 1);
        }
    };
    const retry = () =>
        manual.run(async (signal) => {
            manualContext.current = context;
            setLoading(true);
            try {
                return await load(signal, true);
            } finally {
                if (manualContext.current === context)
                    manualContext.current = null;
            }
        }, "Transfer history refreshed.");
    const move = (next: number, resetTotals = false) => {
        if (next === pageNumber && !resetTotals) return;
        generation.current++;
        setLoading(true);
        setError("");
        setJobs([]);
        setLocalItems([]);
        if (resetTotals) setPage(null);
        setPageNumber(next);
    };
    const latest = new Map(items.map((item) => [item.key, item]));
    const pageItems = new Map(
        localItems.map((item) => [item.key, latest.get(item.key) || item]),
    );
    for (const item of matchHistoryTasks(liveHistoryRows(items), query))
        pageItems.set(item.key, item);
    const visible = visibleHistory(
        jobs,
        [...pageItems.values()],
        filter,
        true,
        true,
    );
    const controls = (
        <div className="transfer-history-toolbar">
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
                                move(1, true);
                                setFilter(entry.key);
                            }
                        }}
                    >
                        {entry.label}
                    </button>
                ))}
            </div>
            <button
                type="button"
                className="secondary-button transfer-history-clear"
                disabled={clearing || manual.refreshing}
                onClick={() => {
                    setClearError("");
                    setConfirmClear(true);
                }}
            >
                <Trash2 size={16} aria-hidden="true" /> Clear history
            </button>
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
                {query
                    ? "No Matching Finished Transfers"
                    : filter === "all"
                      ? pageNumber === 1
                          ? "No Finished Transfers Yet"
                          : "No More Finished Transfers"
                      : "No " +
                        filters.find((entry) => entry.key === filter)!.label +
                        " Transfers"}
            </h2>
            <p>
                {query
                    ? "Try another name or clear the search."
                    : filter === "all"
                      ? "Successful, failed and cancelled transfers appear here."
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
                                    disabled={loading || manual.refreshing}
                                    aria-busy={manual.refreshing}
                                    onClick={() => void retry()}
                                >
                                    {manual.refreshing && <Spinner />}
                                    {manual.refreshing
                                        ? "Refreshing…"
                                        : "Try Again"}
                                </button>
                            </div>
                        )}
                    </>
                }
                finishedEmpty={empty}
            />
            {page && page.total_tasks > 0 && (
                <HistoryPagination
                    page={pageNumber}
                    totalPages={page.total_pages}
                    totalTasks={page.total_tasks}
                    loading={loading}
                    onPageChange={move}
                />
            )}
            {confirmClear && (
                <Modal
                    title="Clear transfer history?"
                    initialFocus="#keep-transfer-history"
                    onClose={() => {
                        if (!clearingRef.current) setConfirmClear(false);
                    }}
                >
                    <p>
                        Remove successful, failed and cancelled transfers from
                        all history pages, including entries for deleted files?
                        This applies to every filter and search result and
                        cannot be undone.
                    </p>
                    <p>
                        Your files, active uploads and transfers waiting for a
                        file will be kept. Folders with unfinished uploads keep
                        their history until the remaining uploads finish.
                    </p>
                    <ErrorNotice message={clearError} />
                    <div className="dialog-actions">
                        <button
                            id="keep-transfer-history"
                            type="button"
                            className="secondary-button"
                            disabled={clearing}
                            onClick={() => setConfirmClear(false)}
                        >
                            Keep history
                        </button>
                        <button
                            type="button"
                            className="danger-button"
                            disabled={clearing}
                            aria-busy={clearing}
                            onClick={() => void clearHistory()}
                        >
                            {clearing && <Spinner />}
                            {clearing ? "Clearing…" : "Clear history"}
                        </button>
                    </div>
                </Modal>
            )}
        </section>
    );
}

function numberedPages(page: number, total: number): (number | "gap")[] {
    if (total <= 7)
        return Array.from({ length: total }, (_, index) => index + 1);
    const start = page <= 3 ? 2 : Math.max(2, Math.min(page - 1, total - 4));
    const end =
        page >= total - 2
            ? total - 1
            : Math.min(total - 1, Math.max(page + 1, 5));
    const pages: (number | "gap")[] = [1];
    if (start > 2) pages.push("gap");
    for (let number = start; number <= end; number++) pages.push(number);
    if (end < total - 1) pages.push("gap");
    pages.push(total);
    return pages;
}

export function HistoryPagination({
    page,
    totalPages,
    totalTasks,
    loading,
    onPageChange,
}: {
    page: number;
    totalPages: number;
    totalTasks: number;
    loading: boolean;
    onPageChange: (page: number) => void;
}) {
    return (
        <nav
            className="transfer-history-pagination"
            aria-label="Finished transfer pages"
            aria-busy={loading}
        >
            <span role="status" aria-live="polite" aria-atomic="true">
                Page {page} of {totalPages} · {totalTasks}{" "}
                {totalTasks === 1 ? "task" : "tasks"}
                {loading ? " · Loading…" : ""}
            </span>
            <div className="transfer-history-page-controls">
                <button
                    type="button"
                    className="secondary-button"
                    aria-label="Previous page of finished transfers"
                    title="Previous page"
                    disabled={loading || page <= 1}
                    onClick={() => onPageChange(page - 1)}
                >
                    <ChevronLeft size={16} aria-hidden="true" />
                </button>
                <div className="transfer-history-page-numbers">
                    {numberedPages(page, totalPages).map((number, index) =>
                        number === "gap" ? (
                            <span key={"gap-" + index} aria-hidden="true">
                                …
                            </span>
                        ) : (
                            <button
                                type="button"
                                key={number}
                                className="secondary-button"
                                aria-label={
                                    "Page " + number + " of finished transfers"
                                }
                                aria-current={
                                    number === page ? "page" : undefined
                                }
                                disabled={loading}
                                onClick={() => onPageChange(number)}
                            >
                                {number}
                            </button>
                        ),
                    )}
                </div>
                <button
                    type="button"
                    className="secondary-button"
                    aria-label="Next page of finished transfers"
                    title="Next page"
                    disabled={loading || page >= totalPages}
                    onClick={() => onPageChange(page + 1)}
                >
                    <ChevronRight size={16} aria-hidden="true" />
                </button>
            </div>
        </nav>
    );
}
