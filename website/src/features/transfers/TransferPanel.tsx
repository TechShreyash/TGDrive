import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
    Upload,
    ChevronDown,
    ChevronRight,
    ChevronUp,
    X,
    RotateCcw,
    Check,
    Pause,
    AlertCircle,
    Folder,
    FolderOpen,
    Clock3,
    FileUp,
    Archive,
    FolderArchive,
} from "lucide-react";
import {
    transfers,
    isTransferActive,
    canCancelTransfer,
    type Transfer,
} from "./transfers";
import { historyStatus } from "./transferHistory";
import { isArchiveTransfer } from "./transferTasks";
import { deviceUploadMessage, needsDeviceUpload } from "./deviceUploadStatus";
import {
    summarizeBatches,
    batchCategory,
    type TransferBatch,
} from "./transferBatches";
import {
    formatBytes,
    formatByteProgress,
    remainingBytesHint,
    getTimezone,
} from "../../shared/types";
import { errorMessage } from "../../shared/api";
import { Modal, Spinner } from "../../shared/components";
import {
    transferDetails,
    transferProgress,
    formatDuration,
    formatProgressPercent,
} from "./progress";
import "./TransferPanel.css";

export default function TransferPanel({
    items,
    onError,
    expandedPage = false,
    onLocate,
    pageContext = "files",
    finishedControls,
    finishedEmpty,
}: {
    items: Transfer[];
    onError: (message: string) => void;
    expandedPage?: boolean;
    pageContext?: string;
    onLocate?: (item: Transfer, folder?: boolean) => void;
    finishedControls?: ReactNode;
    finishedEmpty?: ReactNode;
}) {
    // The Transfers page is global; the floating receipt belongs to this browser.
    items = expandedPage
        ? items
        : items.filter((item) => item.initiatedHere === true);
    const [collapsed, setCollapsed] = useState(
        () =>
            !!window.history.state?.tgdriveReveal ||
            window.matchMedia("(max-width: 720px)").matches ||
            pageContext !== "files",
    );
    const [notice, setNotice] = useState<{
        batchId: string;
        text: string;
        remaining: number;
    } | null>(null);
    const [expandedBatches, setExpandedBatches] = useState<Set<string>>(
        new Set(),
    );
    const [cancelKeys, setCancelKeys] = useState<string[] | null>(null);
    const input = useRef<HTMLInputElement>(null);
    const bulkInput = useRef<HTMLInputElement>(null);
    const folderInput = useRef<HTMLInputElement>(null);
    const resumeKey = useRef("");
    const resumeBatch = useRef("");
    const active = items.filter(isTransferActive).length;
    const unresolved =
        active > 0 || items.some((item) => item.state === "error");
    const [floatingVisible, setFloatingVisible] = useState(unresolved);
    useEffect(() => {
        if (expandedPage) return;
        if (unresolved) {
            setFloatingVisible(true);
            return;
        }
        // A recent upload may show its completion briefly; fetched history stays quiet.
        const timer = setTimeout(() => setFloatingVisible(false), 5000);
        return () => clearTimeout(timer);
    }, [unresolved, expandedPage]);
    useEffect(() => {
        if (!expandedPage && pageContext !== "files") setCollapsed(true);
    }, [pageContext, expandedPage]);
    const [now, setNow] = useState(Date.now);
    useEffect(() => {
        if (!active) return;
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [active]);
    const batches = summarizeBatches(
        items,
        now,
        transfers.batchProgressSnapshot(),
    );
    useEffect(() => {
        if (!notice) return;
        const batch = batches.find((batch) => batch.id === notice.batchId);
        const remaining = batch?.items.filter(
            (item) =>
                !item.jobId && ["awaiting_file", "error"].includes(item.state),
        ).length;
        if (!batch?.pending || remaining !== notice.remaining) setNotice(null);
    }, [items, notice]);
    const deviceUploadsPending =
        items.some(needsDeviceUpload) ||
        transfers.snapshot().some(needsDeviceUpload);
    const deviceMessage = deviceUploadMessage(items, deviceUploadsPending);
    const categories = expandedPage
        ? [
              { key: "running", label: "Running" },
              { key: "queued", label: "Queued" },
              { key: "finished", label: "Finished" },
          ]
        : [
              { key: "running", label: "Running" },
              { key: "queued", label: "Queued" },
              { key: "error", label: "Needs Attention" },
              { key: "completed", label: "Completed" },
              { key: "cancelled", label: "Cancelled" },
          ];
    const groups = categories
        .map((category) => {
            const groupedBatches = batches.filter((batch) =>
                category.key === "finished"
                    ? !["running", "queued"].includes(batchCategory(batch))
                    : batchCategory(batch) === category.key,
            );
            if (category.key === "finished") {
                const created = (batch: TransferBatch) =>
                    Math.min(
                        ...batch.items.map(
                            (item) =>
                                item.batchCreatedAt || item.createdAt || 0,
                        ),
                    );
                groupedBatches.sort(
                    (a, b) =>
                        created(b) - created(a) || a.id.localeCompare(b.id),
                );
            }
            return {
                ...category,
                batches: groupedBatches,
                count: groupedBatches.length,
            };
        })
        .filter(
            (group) =>
                group.count > 0 ||
                (group.key === "finished" && finishedControls),
        );

    const cancellable = items.filter(canCancelTransfer);
    const cancelCount = cancelKeys
        ? batches.filter((batch) =>
              batch.items.some(
                  (item) =>
                      canCancelTransfer(item) && cancelKeys.includes(item.key),
              ),
          ).length
        : 0;
    const pending = batches.filter((batch) => batch.pending).length;
    const hasArchiveOperations = items.some(isArchiveTransfer);
    const operation = hasArchiveOperations ? "transfer" : "upload";
    const heading = batches.some((batch) => batch.active > 0)
        ? pending +
          (pending === 1
              ? " " + operation + " in progress"
              : " " + operation + "s in progress")
        : batches.some((batch) => batch.queued > 0)
          ? pending +
            (pending === 1
                ? " " + operation + " waiting"
                : " " + operation + "s waiting")
          : batches.some((batch) => batch.failed || batch.awaiting)
            ? hasArchiveOperations
                ? "Transfers Need Attention"
                : "Uploads Need Attention"
            : "Transfers";
    const folderPickerSupported =
        "webkitdirectory" in document.createElement("input");
    const run = (task: Promise<unknown>) => {
        void task.catch((error) => onError(errorMessage(error)));
    };
    const reselect = async (files: File[]) => {
        const batchId = resumeBatch.current;
        const result = await transfers.resumeBatchFiles(batchId, files);
        setNotice({
            batchId,
            remaining: result.remaining,
            text:
                result.matched +
                (result.matched === 1 ? " file" : " files") +
                " added to the resume queue." +
                (result.skipped
                    ? " " +
                      result.skipped +
                      (result.skipped === 1 ? " file did" : " files did") +
                      " not need resuming."
                    : "") +
                (result.remaining
                    ? " " +
                      result.remaining +
                      " still need their original files."
                    : "") +
                (result.unmatched
                    ? " " +
                      result.unmatched +
                      " selected files did not match this upload."
                    : "") +
                (result.errors.length ? " " + result.errors[0] : ""),
        });
    };
    if (!expandedPage && !floatingVisible) return null;
    if (!items.length && !finishedControls)
        return expandedPage ? (
            <div className="empty-state">
                <Upload size={40} />
                <h2>No Transfers Yet</h2>
                <p>Uploads started from a folder in My Files appear here.</p>
            </div>
        ) : null;
    return (
        <section
            id="transfer-dialog"
            className={
                "transfer-center " + (expandedPage ? "transfer-page" : "")
            }
            aria-label="Transfers"
        >
            <header
                className="transfer-heading"
                hidden={expandedPage && !cancellable.length}
            >
                <h2
                    id="transfer-summary"
                    className={expandedPage ? "sr-only" : undefined}
                >
                    {(!collapsed || expandedPage) && <Upload size={18} />}
                    {expandedPage ? (
                        batches.length +
                        (batches.length === 1
                            ? " transfer shown"
                            : " transfers shown")
                    ) : collapsed ? (
                        <CollapsedTransferSummary
                            batches={batches}
                            deviceMessage={deviceMessage}
                        />
                    ) : (
                        heading
                    )}
                </h2>
                <div className="inline-actions">
                    {cancellable.length > 0 && (
                        <button
                            className="text-button"
                            onClick={() =>
                                setCancelKeys(
                                    cancellable.map((item) => item.key),
                                )
                            }
                        >
                            Cancel All
                        </button>
                    )}
                    {!expandedPage && (
                        <button
                            id="transfer-toggle"
                            className="text-button"
                            onClick={() => setCollapsed(!collapsed)}
                            aria-expanded={!collapsed}
                            aria-controls="transfer-list"
                            aria-label={
                                collapsed
                                    ? "Show Transfer Details"
                                    : "Hide Transfer Details"
                            }
                            title={
                                collapsed
                                    ? "Show Transfer Details"
                                    : "Hide details; transfers keep running"
                            }
                        >
                            {collapsed ? "Show" : "Hide"}
                            {collapsed ? (
                                <ChevronUp size={15} />
                            ) : (
                                <ChevronDown size={15} />
                            )}
                        </button>
                    )}
                </div>
            </header>
            <div
                id="transfer-list"
                className="transfer-list"
                hidden={collapsed && !expandedPage}
            >
                {groups.map((group) => (
                    <section
                        className="transfer-category"
                        data-category={group.key}
                        key={group.key}
                        aria-label={group.label}
                    >
                        <h3
                            className="transfer-category-title"
                            hidden={!expandedPage && groups.length === 1}
                        >
                            {group.label}
                            <span>{group.count}</span>
                        </h3>
                        {group.key === "finished" && finishedControls}
                        {group.key === "finished" &&
                            !group.count &&
                            finishedEmpty}
                        <div role="list">
                            {group.batches.map((batch) => (
                                <BatchTransfer
                                    key={batch.id}
                                    batch={batch}
                                    resumeNotice={
                                        notice?.batchId === batch.id
                                            ? {
                                                  text: notice.text,
                                                  dismiss: () =>
                                                      setNotice(null),
                                              }
                                            : undefined
                                    }
                                    deviceUploadsPending={deviceUploadsPending}
                                    expanded={expandedBatches.has(batch.id)}
                                    onToggleExpanded={() =>
                                        setExpandedBatches((previous) => {
                                            const next = new Set(
                                                [...previous].filter((id) =>
                                                    batches.some(
                                                        (current) =>
                                                            current.id === id,
                                                    ),
                                                ),
                                            );
                                            if (next.has(batch.id))
                                                next.delete(batch.id);
                                            else next.add(batch.id);
                                            return next;
                                        })
                                    }
                                    onLocate={
                                        onLocate
                                            ? (item, folder) => {
                                                  setCollapsed(true);
                                                  onLocate(item, folder);
                                              }
                                            : undefined
                                    }
                                    showTimestamp={expandedPage}
                                    now={now}
                                    run={run}
                                    onChooseFile={(key) => {
                                        setNotice(null);
                                        resumeKey.current = key;
                                        input.current?.click();
                                    }}
                                    onChooseBatch={() => {
                                        setNotice(null);
                                        resumeBatch.current = batch.id;
                                        if (batch.folder)
                                            folderInput.current?.click();
                                        else bulkInput.current?.click();
                                    }}
                                    folderPickerSupported={
                                        folderPickerSupported
                                    }
                                />
                            ))}
                        </div>
                    </section>
                ))}
            </div>
            {!expandedPage &&
                !collapsed &&
                batches.some((batch) => !batch.pending) && (
                    <div className="transfer-footer">
                        <button
                            id="transfer-clear"
                            className="text-button"
                            onClick={() => transfers.clearFinished()}
                            title="Remove completed and cancelled transfers from this list"
                        >
                            Clear Finished
                        </button>
                    </div>
                )}
            {cancelKeys !== null && (
                <Modal
                    title={
                        hasArchiveOperations
                            ? "Cancel All Transfers?"
                            : "Cancel All Uploads?"
                    }
                    initialFocus="[data-keep-uploading]"
                    onClose={() => setCancelKeys(null)}
                >
                    <p>
                        Cancel {cancelCount} unfinished{" "}
                        {cancelCount === 1 ? operation : operation + "s"}? Files
                        already in your drive will be kept.
                    </p>
                    <div className="dialog-actions">
                        <button
                            className="secondary-button"
                            data-keep-uploading
                            onClick={() => setCancelKeys(null)}
                        >
                            {hasArchiveOperations
                                ? "Keep Running"
                                : "Keep Uploading"}
                        </button>
                        <button
                            className="danger-button"
                            disabled={!cancelCount}
                            onClick={() => {
                                const keys = cancelKeys;
                                setCancelKeys(null);
                                run(transfers.cancelAll(keys));
                            }}
                        >
                            {hasArchiveOperations
                                ? "Cancel Transfers"
                                : "Cancel Uploads"}
                        </button>
                    </div>
                </Modal>
            )}
            {transfers.recoveryWarning && (
                <p
                    className="transfer-help transfer-storage-warning"
                    role="status"
                >
                    {transfers.recoveryWarning}
                </p>
            )}
            <input
                ref={input}
                type="file"
                hidden
                aria-label="Choose Original File to Resume"
                onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) run(transfers.resume(resumeKey.current, file));
                    event.target.value = "";
                }}
            />
            <input
                ref={bulkInput}
                type="file"
                multiple
                hidden
                aria-label="Choose Original Files to Resume"
                onChange={(event) => {
                    const files = Array.from(event.target.files || []);
                    if (files.length) run(reselect(files));
                    event.target.value = "";
                }}
            />
            <input
                ref={(node) => {
                    folderInput.current = node;
                    node?.setAttribute("webkitdirectory", "");
                }}
                type="file"
                multiple
                hidden
                aria-label="Choose Original Folder to Resume"
                onChange={(event) => {
                    const files = Array.from(event.target.files || []);
                    if (files.length) run(reselect(files));
                    event.target.value = "";
                }}
            />
        </section>
    );
}

export function CollapsedTransferSummary({
    batches,
    deviceMessage,
}: {
    batches: TransferBatch[];
    deviceMessage: string | null;
}) {
    const hasArchiveOperations = batches.some((batch) =>
        batch.items.some(isArchiveTransfer),
    );
    const uploading = batches.filter(
        (batch) =>
            batch.active >
            batch.items.filter((item) => item.state === "cancelling").length,
    ).length;
    const queued = batches.filter(
        (batch) => !batch.active && batch.queued > 0,
    ).length;
    const attention = batches.filter(
        (batch) => batch.failed || batch.awaiting,
    ).length;
    const cancelling = batches.filter((batch) =>
        batch.items.some((item) => item.state === "cancelling"),
    ).length;
    const complete =
        batches.length > 0 &&
        batches.every((batch) => batch.completed === batch.count);
    const cancelled =
        batches.length > 0 &&
        batches.every((batch) => batch.cancelled === batch.count);
    const pending = [
        uploading
            ? uploading + (hasArchiveOperations ? " running" : " uploading")
            : "",
        queued ? queued + " queued" : "",
        cancelling ? cancelling + " cancelling" : "",
        attention
            ? attention +
              (attention === 1 ? " needs attention" : " need attention")
            : "",
    ]
        .filter(Boolean)
        .join(" · ");
    const summary =
        pending ||
        (complete
            ? hasArchiveOperations
                ? "Transfers complete"
                : "Uploads complete"
            : cancelled
              ? hasArchiveOperations
                  ? "Transfers cancelled"
                  : "Uploads cancelled"
              : batches.length
                ? hasArchiveOperations
                    ? "Transfers finished"
                    : "Uploads finished"
                : "Transfers");
    const StatusIcon = attention
        ? AlertCircle
        : uploading
          ? Upload
          : queued
            ? Clock3
            : cancelling || cancelled
              ? X
              : complete
                ? Check
                : Upload;
    const guidance =
        attention && !uploading && !queued && !cancelling
            ? "Open details to continue"
            : deviceMessage;
    return (
        <span
            className="transfer-collapsed-status"
            role="status"
            aria-atomic="true"
        >
            <StatusIcon size={18} aria-hidden="true" />
            <span className="transfer-collapsed-copy">
                <span>{summary}</span>
                {guidance && (
                    <span className="transfer-collapsed-guidance">
                        {guidance}
                    </span>
                )}
            </span>
        </span>
    );
}

function batchStatus(batch: TransferBatch, deviceStage: boolean) {
    const counts = [
        deviceStage
            ? batch.device.pending
                ? "Uploading from device"
                : "Received"
            : "Uploading to Telegram",
    ];
    if (batch.count > 1 && (!deviceStage || batch.device.pending))
        counts.push(
            deviceStage
                ? batch.device.received +
                      " of " +
                      batch.device.count +
                      " received"
                : batch.completed + " of " + batch.count + " uploaded",
        );
    const attention = batch.device.pending
        ? batch.items.filter(needsDeviceUpload)
        : batch.items;
    const failed = attention.filter((item) => item.state === "error").length;
    const awaiting = attention.filter(
        (item) => item.state === "awaiting_file",
    ).length;
    if (failed) counts.push(failed + " failed");
    if (awaiting) counts.push(awaiting + " need originals");
    if (batch.cancelled) counts.push(batch.cancelled + " cancelled");
    return counts.join(" · ");
}

export function BatchTransfer({
    batch,
    deviceUploadsPending,
    expanded,
    onToggleExpanded,
    now,
    run,
    onChooseFile,
    onChooseBatch,
    folderPickerSupported,
    showTimestamp = false,
    onLocate,
    resumeNotice,
}: {
    onLocate?: (item: Transfer, folder?: boolean) => void;
    batch: TransferBatch;
    deviceUploadsPending: boolean;
    expanded: boolean;
    onToggleExpanded: () => void;
    showTimestamp?: boolean;
    now: number;
    run: (task: Promise<unknown>) => void;
    onChooseFile: (key: string) => void;
    onChooseBatch: () => void;
    folderPickerSupported: boolean;
    resumeNotice?: { text: string; dismiss: () => void };
}) {
    const [filter, setFilter] = useState("all");
    const [limit, setLimit] = useState(20);
    const listId = useId();
    const hasFolder = batch.folder;
    const deviceMessage = deviceUploadMessage(
        batch.items,
        deviceUploadsPending,
    );
    // Keep the browser's pending uploads in one visible phase, even when a
    // different batch has already handed its files to the server.
    const deviceStage =
        batch.device.pending ||
        (deviceUploadsPending &&
            batch.pending &&
            batch.items.some(
                (item) =>
                    !item.historyOnly &&
                    !["remote", "torrent", "delete"].includes(item.kind || "") &&
                    !isArchiveTransfer(item),
            ));
    const receiptComplete = deviceStage && !batch.device.pending;
    if (!hasFolder)
        return (
            <>
                {batch.items.map((item) => (
                    <TransferRow
                        key={item.key}
                        item={item}
                        deviceUploadsPending={deviceUploadsPending}
                        deviceStage={deviceStage}
                        onLocate={onLocate}
                        showTimestamp={showTimestamp}
                        now={now}
                        run={run}
                        onChooseFile={onChooseFile}
                    />
                ))}
            </>
        );
    const folderItem = batch.items.find((item) => item.state === "completed");
    const canShowFolder =
        hasFolder &&
        !!onLocate &&
        !!folderItem &&
        batch.items.every((item) => item.state === "completed");
    const finishedTimes = batch.items
        .map((item) => item.finishedAt)
        .filter(
            (value): value is number =>
                typeof value === "number" &&
                Number.isFinite(value) &&
                value > 0 &&
                value <= 8.64e15,
        );
    const finishedAt =
        !batch.pending && finishedTimes.length === batch.count
            ? Math.max(...finishedTimes)
            : undefined;
    const locations = [
        ...new Set(
            batch.items
                .map((item) =>
                    item.location?.available
                        ? item.location.folder_label
                        : undefined,
                )
                .filter((label): label is string => !!label),
        ),
    ];
    let locationLabel =
        locations.length === 1
            ? locations[0]
            : locations.length > 1
              ? locations.length + " folders"
              : batch.items.every((item) => item.destination === "/")
                ? "My Files"
                : "";
    if (hasFolder) {
        const source = batch.items.find(
            (item) => item.location?.available && item.location.folder_label,
        );
        if (source?.location?.folder_label) {
            const root = source.folderUpload?.root_path
                .split("/")
                .filter(Boolean);
            const destination = source.destination.split("/").filter(Boolean);
            const depth = source.relativePath
                ? Math.max(0, source.relativePath.split("/").length - 2)
                : root &&
                    root.every((name, index) => destination[index] === name)
                  ? destination.length - root.length
                  : 0;
            const names = source.location.folder_label.split(" / ");
            locationLabel = names
                .slice(0, Math.max(1, names.length - depth))
                .join(" / ");
        }
    }
    if (batch.cancelled === batch.count) locationLabel = "";
    const showProgress =
        batch.pending && batch.active + batch.queued > 0 && !receiptComplete;
    const displayTotal =
        deviceStage && batch.device.count > 0
            ? batch.device.total
            : batch.totalBytes;
    const displayRate = receiptComplete ? undefined : batch.displayRate;
    const displayEta = receiptComplete ? undefined : batch.displayEta;
    const received = (item: Transfer) =>
        !needsDeviceUpload(item) && item.state !== "cancelled";
    const activeFile = (item: Transfer) =>
        isTransferActive(item) &&
        item.state !== "awaiting_file" &&
        (!deviceStage || needsDeviceUpload(item));
    const activeCount = batch.items.filter(activeFile).length;
    const receivedCount = batch.items.filter(received).length;
    const mixedStatuses =
        [
            activeCount,
            batch.failed + batch.awaiting,
            deviceStage ? receivedCount : batch.completed,
            batch.cancelled,
        ].filter((count) => count > 0).length > 1;
    const canCancel = batch.items.some(
        (item) =>
            canCancelTransfer(item) &&
            (isTransferActive(item) || item.state === "error"),
    );
    const needsFiles = batch.items.some(
        (item) =>
            item.state === "awaiting_file" ||
            (item.state === "error" && !item.jobId && !item.file),
    );
    const filtered = batch.items.filter(
        (item) =>
            !mixedStatuses ||
            filter === "all" ||
            (filter === "active" && activeFile(item)) ||
            (filter === "attention" &&
                (item.state === "error" || item.state === "awaiting_file")) ||
            (filter === "completed" &&
                (deviceStage ? received(item) : item.state === "completed")) ||
            (filter === "cancelled" && item.state === "cancelled"),
    );
    const filters = [
        { key: "all", label: "All", count: batch.count },
        { key: "active", label: "Active", count: activeCount },
        {
            key: "attention",
            label: "Needs Attention",
            count: batch.failed + batch.awaiting,
        },
        {
            key: "completed",
            label: deviceStage ? "Received" : "Done",
            count: deviceStage ? receivedCount : batch.completed,
        },
        { key: "cancelled", label: "Cancelled", count: batch.cancelled },
    ].filter(
        (entry) =>
            entry.key === "all" || entry.count > 0 || entry.key === filter,
    );
    return (
        <article
            className="transfer-batch"
            role="listitem"
            data-batch-id={batch.id}
            data-cancelled={batch.cancelled === batch.count || undefined}
            data-folder={hasFolder || undefined}
            data-expanded={expanded}
            data-stage={deviceStage ? "device" : "telegram"}
            data-status={
                !batch.pending
                    ? "finished"
                    : batch.failed || batch.awaiting
                      ? "attention"
                      : "active"
            }
        >
            {resumeNotice && (
                <div className="transfer-notice">
                    <p role="status">
                        {batch.name}: {resumeNotice.text}
                    </p>
                    <button
                        type="button"
                        className="icon-button"
                        aria-label={"Dismiss resume result for " + batch.name}
                        onClick={resumeNotice.dismiss}
                    >
                        <X size={16} aria-hidden="true" />
                    </button>
                </div>
            )}
            <div className="transfer-batch-summary">
                <div className="transfer-batch-title">
                    <button
                        type="button"
                        className="transfer-batch-toggle"
                        aria-label={expanded ? "Hide Files" : "Show Files"}
                        aria-describedby={listId + "-name"}
                        aria-expanded={expanded}
                        aria-controls={listId}
                        onClick={onToggleExpanded}
                        onDoubleClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                        }}
                    >
                        {expanded ? (
                            <ChevronDown size={15} />
                        ) : (
                            <ChevronRight size={15} />
                        )}
                        <span
                            className="transfer-group-icon"
                            aria-hidden="true"
                        >
                            <Folder size={20} />
                        </span>
                        <strong
                            id={listId + "-name"}
                            className="transfer-batch-name"
                            title={batch.name}
                        >
                            {batch.name}
                        </strong>
                        {hasFolder && (
                            <span className="transfer-batch-count">
                                {batch.count}{" "}
                                {batch.count === 1 ? "file" : "files"}
                            </span>
                        )}
                    </button>
                    {showProgress && batch.progress !== undefined && (
                        <span className="transfer-batch-percent">
                            {formatProgressPercent(batch.progress)}
                        </span>
                    )}
                </div>
                {locationLabel && (
                    <span
                        className="transfer-batch-location"
                        title={locationLabel}
                    >
                        {locationLabel}
                    </span>
                )}
                {batch.pending && (
                    <p className="transfer-batch-status" aria-live="polite">
                        {batchStatus(batch, deviceStage)}
                        {deviceMessage && (
                            <span className="transfer-device-status">
                                {" "}
                                · {deviceMessage}
                            </span>
                        )}
                    </p>
                )}
                {showTimestamp && finishedAt && (
                    <time
                        className="transfer-batch-time"
                        dateTime={new Date(finishedAt).toISOString()}
                        title={new Intl.DateTimeFormat(undefined, {
                            dateStyle: "full",
                            timeStyle: "short",
                            timeZone: getTimezone(),
                        }).format(finishedAt)}
                    >
                        {new Intl.DateTimeFormat(undefined, {
                            dateStyle: "medium",
                            timeStyle: "short",
                            timeZone: getTimezone(),
                        }).format(finishedAt)}
                    </time>
                )}
                {canShowFolder && folderItem && onLocate && (
                    <button
                        className="icon-button transfer-batch-locate"
                        aria-label={"Show " + batch.name + " folder"}
                        title="Show Folder"
                        onClick={() => onLocate(folderItem, true)}
                    >
                        <FolderOpen size={18} />
                    </button>
                )}
                {canCancel && (
                    <button
                        className="icon-button transfer-batch-cancel"
                        aria-label="Cancel Upload"
                        title="Cancel Upload"
                        onClick={() => run(transfers.cancelBatch(batch.id))}
                    >
                        <X size={18} />
                    </button>
                )}
                {showProgress && (
                    <progress
                        aria-label={
                            (deviceStage
                                ? "Device upload"
                                : "Telegram upload") +
                            " progress for " +
                            batch.name
                        }
                        max={1}
                        value={batch.progress}
                    />
                )}
                <div className="transfer-batch-metrics">
                    {displayTotal !== undefined && (
                        <span className="transfer-batch-size">
                            {batch.pending && !receiptComplete
                                ? formatByteProgress(
                                      batch.displayBytes,
                                      displayTotal,
                                  )
                                : formatBytes(displayTotal)}
                        </span>
                    )}
                    <span className="transfer-batch-throughput">
                        {displayRate && (
                            <span>{formatBytes(displayRate)}/s</span>
                        )}
                        {displayEta !== undefined && (
                            <span
                                title={
                                    deviceStage
                                        ? "Estimated upload time from your device. Server verification may take longer."
                                        : "Estimated upload time to Telegram. Publishing and retries may take longer."
                                }
                            >
                                About{" "}
                                {formatDuration(
                                    Math.max(1, Math.ceil(displayEta)),
                                )}{" "}
                                remaining
                            </span>
                        )}
                    </span>
                </div>
                {(needsFiles ||
                    batch.items.some(
                        (item) =>
                            item.state === "error" &&
                            !item.historyOnly &&
                            (item.jobId || item.file),
                    )) && (
                    <div className="transfer-batch-actions">
                        {needsFiles &&
                            (!hasFolder || folderPickerSupported) && (
                                <button
                                    type="button"
                                    className="secondary-button transfer-recovery-button"
                                    onClick={onChooseBatch}
                                >
                                    <FileUp size={15} />
                                    {hasFolder
                                        ? "Choose Folder"
                                        : "Choose Files"}
                                </button>
                            )}
                        {batch.items.some(
                            (item) =>
                                item.state === "error" &&
                                !item.historyOnly &&
                                (item.jobId || item.file),
                        ) && (
                            <button
                                type="button"
                                className="text-button"
                                onClick={() =>
                                    run(transfers.retryBatch(batch.id))
                                }
                            >
                                <RotateCcw size={14} />
                                Retry Failed
                            </button>
                        )}
                    </div>
                )}
            </div>
            {expanded && (
                <div className="transfer-batch-files" id={listId}>
                    {mixedStatuses && (
                        <div className="transfer-files-toolbar">
                            <div
                                className="transfer-filters"
                                role="group"
                                aria-label={"Files shown for " + batch.name}
                            >
                                {filters.map((entry) => (
                                    <button
                                        key={entry.key}
                                        type="button"
                                        data-filter={entry.key}
                                        aria-pressed={filter === entry.key}
                                        onClick={() => {
                                            setFilter(entry.key);
                                            setLimit(20);
                                        }}
                                    >
                                        {entry.label} <span>{entry.count}</span>
                                    </button>
                                ))}
                            </div>
                        </div>
                    )}
                    <div role="list">
                        {filtered.slice(0, limit).map((item) => (
                            <TransferRow
                                key={item.key}
                                item={item}
                                onLocate={onLocate}
                                compactChild
                                deviceStage={deviceStage}
                                groupLocation={
                                    hasFolder ? locationLabel : undefined
                                }
                                showTimestamp={false}
                                now={now}
                                run={run}
                                onChooseFile={onChooseFile}
                            />
                        ))}
                    </div>
                    {!filtered.length && (
                        <p className="transfer-no-files">
                            No files in this view.
                        </p>
                    )}
                    {filtered.length > limit && (
                        <button
                            type="button"
                            className="text-button transfer-show-more"
                            onClick={() => setLimit((value) => value + 20)}
                        >
                            Show more ({filtered.length - limit} remaining)
                        </button>
                    )}
                </div>
            )}
        </article>
    );
}

function TransferRow({
    item,
    deviceUploadsPending,
    now,
    showTimestamp = false,
    compactChild = false,
    deviceStage = false,
    groupLocation,
    onLocate,
    run,
    onChooseFile,
}: {
    compactChild?: boolean;
    deviceStage?: boolean;
    groupLocation?: string;
    onLocate?: (item: Transfer, folder?: boolean) => void;
    item: Transfer;
    deviceUploadsPending?: boolean;
    now: number;
    showTimestamp?: boolean;
    run: (task: Promise<unknown>) => void;
    onChooseFile: (key: string) => void;
}) {
    const fullLocation = item.location?.available
        ? item.location.folder_label || ""
        : item.location?.reason || "";
    const displayLocation =
        groupLocation && fullLocation === groupLocation
            ? ""
            : groupLocation && fullLocation.startsWith(groupLocation + " / ")
              ? fullLocation.slice(groupLocation.length + 3)
              : fullLocation;
    const relativeDirectory = item.relativePath
        ?.split("/")
        .slice(1, -1)
        .join(" / ");
    const meter = usePresentedProgress(item);
    const status = historyStatus(item);
    const finished = status === "completed" || status === "cancelled";
    const received =
        deviceStage && !needsDeviceUpload(item) && item.state !== "cancelled";
    const checkingSource =
        ["remote", "torrent"].includes(item.kind || "") &&
        ["checking_source", "source_retry_wait", "torrent_metadata"].includes(
            item.stage || "",
        ) &&
        item.state === "uploading";
    const sourceWaiting =
        checkingSource &&
        [
            "Checking source…",
            "Starting URL import…",
            "Starting torrent import…",
            "Reading torrent metadata…",
        ].includes(item.phase) &&
        item.serverStatus !== "queued" &&
        now - (item.startedAt || item.createdAt || now) >= 5000;
    const deviceMessage =
        !compactChild && deviceUploadMessage([item], deviceUploadsPending);
    const liveMetrics =
        !received &&
        !finished &&
        status !== "queued" &&
        !["error", "awaiting_file"].includes(item.state);
    const showPercent =
        !received &&
        !finished &&
        status !== "queued" &&
        meter.fraction !== undefined;
    const needsOriginal =
        !["remote", "torrent"].includes(item.kind || "") &&
        (item.state === "awaiting_file" ||
            (item.state === "error" && !item.jobId && !item.file));
    const canLocate =
        !!onLocate &&
        item.state === "completed" &&
        item.location?.available !== false;
    const recordedAt = item.finishedAt || item.createdAt;
    const validRecordedAt =
        typeof recordedAt === "number" &&
        Number.isFinite(recordedAt) &&
        recordedAt > 0 &&
        recordedAt <= 8.64e15;
    return (
        <article
            className={
                "transfer-item" + (compactChild ? " transfer-child" : "")
            }
            role="listitem"
            data-status={item.state}
            data-live-metrics={liveMetrics || undefined}
            onDoubleClick={(event) => {
                if (
                    canLocate &&
                    !(event.target as HTMLElement).closest("button,input,a")
                )
                    onLocate?.(item);
            }}
        >
            <div className="transfer-state-icon">
                {item.state === "error" ? (
                    <AlertCircle size={19} />
                ) : received || item.state === "completed" ? (
                    <Check size={19} />
                ) : item.state === "awaiting_file" ? (
                    <Pause size={19} />
                ) : item.state === "cancelled" ? (
                    <X size={19} />
                ) : status === "queued" ? (
                    <Clock3 size={19} />
                ) : item.kind === "archive" ? (
                    <Archive size={19} />
                ) : item.kind === "extract" ? (
                    <FolderArchive size={19} />
                ) : (
                    <Upload size={19} />
                )}
            </div>
            <div className="transfer-copy">
                <div className="transfer-item-title">
                    <strong className="transfer-item-name" title={item.name}>
                        {item.name}
                    </strong>
                    {showPercent && !liveMetrics && (
                        <span className="transfer-batch-percent">
                            {formatProgressPercent(meter.fraction!)}
                        </span>
                    )}
                </div>
                {item.relativePath &&
                    !item.location?.available &&
                    (!compactChild || !!relativeDirectory) && (
                        <span
                            className="transfer-relative-path"
                            title={item.relativePath}
                        >
                            {compactChild
                                ? relativeDirectory
                                : item.relativePath}
                        </span>
                    )}
                {needsOriginal ? (
                    <div className="transfer-recovery">
                        <span className="transfer-recovery-message">
                            Choose the original file to resume
                        </span>
                        <button
                            className="secondary-button transfer-recovery-button"
                            onClick={() => onChooseFile(item.key)}
                        >
                            <FileUp size={16} aria-hidden="true" /> Choose File
                        </button>
                    </div>
                ) : (
                    (received || !finished) && (
                        <span
                            className={
                                "transfer-phase" +
                                (item.state === "verifying" || checkingSource
                                    ? " transfer-verification"
                                    : "")
                            }
                            role={checkingSource ? "status" : undefined}
                        >
                            {!received &&
                                (item.state === "verifying" ||
                                    checkingSource) && <Spinner />}
                            {received ? "Received" : item.phase}
                        </span>
                    )
                )}
                {item.state !== "cancelled" &&
                    item.location &&
                    displayLocation && (
                        <span
                            className="transfer-location"
                            title={
                                item.location.display_path ||
                                item.location.reason
                            }
                        >
                            {displayLocation}
                        </span>
                    )}
                {!checkingSource && (
                    <div className="transfer-row-details">
                        {liveMetrics && (
                            <TransferMetrics item={item} now={now} />
                        )}
                        {showPercent && liveMetrics && (
                            <span className="transfer-batch-percent">
                                {formatProgressPercent(meter.fraction!)}
                            </span>
                        )}
                        <span
                            className="transfer-bytes"
                            title={
                                !received && item.state === "receiving"
                                    ? "Bytes sent by this browser. Resume positions are saved only after the server confirms each chunk."
                                    : !received &&
                                        item.jobId &&
                                        ["uploading", "finalizing"].includes(
                                            item.stage || "",
                                        ) &&
                                        !finished
                                      ? "Bytes sent to Telegram, including parts still uploading. Each part appears in the channel after it finishes."
                                      : undefined
                            }
                        >
                            {received || finished || status === "queued"
                                ? formatBytes(item.size || item.total)
                                : formatByteProgress(
                                      meter.current,
                                      meter.total,
                                  )}
                        </span>
                        {!received &&
                            !liveMetrics &&
                            !finished &&
                            status !== "queued" && (
                                <TransferMetrics item={item} now={now} />
                            )}
                        {showTimestamp && finished && validRecordedAt && (
                            <time
                                className="transfer-history-time"
                                dateTime={new Date(recordedAt).toISOString()}
                                title={new Intl.DateTimeFormat(undefined, {
                                    dateStyle: "full",
                                    timeStyle: "short",
                                    timeZone: getTimezone(),
                                }).format(recordedAt)}
                            >
                                {new Intl.DateTimeFormat(undefined, {
                                    dateStyle: "medium",
                                    timeStyle: "short",
                                    timeZone: getTimezone(),
                                }).format(recordedAt)}
                            </time>
                        )}
                    </div>
                )}
                {checkingSource && (
                    <div
                        className="transfer-precheck-progress"
                        role="progressbar"
                        aria-label={item.phase + " for " + item.name}
                    >
                        <span />
                    </div>
                )}
                {liveMetrics && !checkingSource && (
                    <progress
                        aria-label={item.phase + " for " + item.name}
                        max={1}
                        value={meter.fraction}
                    />
                )}
                {sourceWaiting && (
                    <span className="transfer-source-wait">
                        {item.jobId
                            ? "The source is taking longer to respond. You can keep browsing."
                            : "Waiting for server confirmation. You can keep browsing."}
                    </span>
                )}
                {deviceMessage && (
                    <span className="transfer-device-status" role="status">
                        {deviceMessage}
                    </span>
                )}
                {item.state !== "cancelled" && item.error && (
                    <span className="transfer-error">{item.error}</span>
                )}
            </div>
            <div className="transfer-item-actions">
                {canLocate && (
                    <button
                        className="icon-button"
                        aria-label={"Show " + item.name + " in folder"}
                        title="Show in Folder"
                        onClick={() => onLocate?.(item)}
                    >
                        <FolderOpen size={18} />
                    </button>
                )}
                {item.state === "error" &&
                    !item.historyOnly &&
                    !needsOriginal && (
                        <button
                            className="icon-button"
                            aria-label={"Retry " + item.name}
                            onClick={() => run(transfers.retry(item.key))}
                        >
                            <RotateCcw size={17} />
                        </button>
                    )}
                {canCancelTransfer(item) &&
                    (isTransferActive(item) || item.state === "error") && (
                        <button
                            className="icon-button"
                            aria-label={"Cancel " + item.name}
                            onClick={() => run(transfers.cancel(item.key))}
                        >
                            <X size={18} />
                        </button>
                    )}
            </div>
        </article>
    );
}

/** Ease toward measured bytes only; a stalled transport never advances on a timer. */
function usePresentedProgress(item: Transfer) {
    const measured = transferProgress(item);
    const stage = item.state + ":" + (item.stage || "");
    const [display, setDisplay] = useState(() => ({
        stage,
        total: measured.total,
        current: measured.current,
    }));
    const shown = useRef(display);
    useEffect(() => {
        const target = {
            stage,
            total: measured.total,
            current: measured.current,
        };
        const previous = shown.current;
        if (
            previous.stage !== stage ||
            previous.total !== target.total ||
            target.current <= previous.current ||
            window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ) {
            shown.current = target;
            setDisplay(target);
            return;
        }
        const start = performance.now();
        let frame = 0;
        const tick = (now: number) => {
            const elapsed = Math.min(1, (now - start) / 180);
            const fraction = 1 - (1 - elapsed) ** 3;
            const next = {
                ...target,
                current:
                    elapsed === 1
                        ? target.current
                        : previous.current +
                          (target.current - previous.current) * fraction,
            };
            shown.current = next;
            setDisplay(next);
            if (elapsed < 1) frame = requestAnimationFrame(tick);
        };
        frame = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(frame);
    }, [stage, measured.current, measured.total]);
    // Stage changes and retries never show bytes carried over from the previous stage.
    if (display.stage !== stage || display.total !== measured.total)
        return measured;
    return transferProgress({
        ...item,
        current: Math.min(display.current, measured.current),
        sentCurrent: undefined,
    });
}

export function TransferMetrics({
    item,
    now,
}: {
    item: Transfer;
    now: number;
}) {
    const details = transferDetails(item, now);
    const meter = transferProgress(item);
    const remaining =
        ["receiving", "uploading"].includes(item.state) &&
        ![
            "queued",
            "retry_wait",
            "source_retry_wait",
            "waiting_for_telegram",
        ].includes(item.stage || "")
            ? remainingBytesHint(meter.current, meter.total)
            : "";
    return (
        <span className="transfer-metrics" data-timing={details.timing}>
            {remaining && <span>{remaining}</span>}
            {details.rate ? (
                <span
                    className="transfer-speed"
                    title="Speed during this stage"
                >
                    {formatBytes(details.rate)}/s
                </span>
            ) : null}
            {details.eta !== undefined && (
                <span
                    className="transfer-eta"
                    title={
                        isArchiveTransfer(item)
                            ? "Estimated time for this stage. Saving files may take longer."
                            : "Estimated upload time for this stage. Verification and publishing may take longer."
                    }
                >
                    About {formatDuration(Math.max(1, Math.ceil(details.eta)))}{" "}
                    remaining
                </span>
            )}
            {details.timing === "stalled" && (
                <span className="transfer-timing-state">
                    No recent progress
                </span>
            )}
            {details.timing === "waiting" && (
                <span className="transfer-timing-state">
                    {item.stage === "source_retry_wait"
                        ? "Waiting to retry source"
                        : "Waiting for Telegram"}
                </span>
            )}
            {details.timing === "finishing" && (
                <span className="transfer-timing-state">
                    Finishing this stage…
                </span>
            )}
        </span>
    );
}
