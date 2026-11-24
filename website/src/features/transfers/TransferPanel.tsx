import {
    useCallback,
    useEffect,
    useId,
    useRef,
    useState,
    type ReactNode,
    type CSSProperties,
} from "react";
import { PasswordInput } from "@mantine/core";
import {
    Upload,
    CloudUpload,
    Download,
    Layers,
    Trash2,
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
import {
    botCleanupPending,
    transferBlocksRetry,
    isArchiveTransfer,
    isServerImport,
} from "./transferTasks";
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
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import {
    transferDetails,
    transferStageProgress,
    formatDuration,
    formatProgressPercent,
} from "./progress";
import "./TransferPanel.css";
import TorrentFilePicker from "./TorrentFilePicker";
import { PdfPasswordFields } from "../files/PdfPasswordDialog";

export default function TransferPanel({
    items,
    onError,
    expandedPage = false,
    onLocate,
    onOpenTransfers,
    pageContext = "files",
    collapsed: collapsedChoice,
    onCollapsedChange,
    finishedControls,
    finishedCount,
    onBrowseFiles,
    finishedEmpty,
}: {
    items: Transfer[];
    onError: (message: string) => void;
    expandedPage?: boolean;
    pageContext?: string;
    collapsed?: boolean;
    onCollapsedChange?: (collapsed: boolean) => void;
    onOpenTransfers?: () => void;
    onLocate?: (item: Transfer, folder?: boolean) => void;
    finishedControls?: ReactNode;
    finishedCount?: number;
    onBrowseFiles?: () => void;
    finishedEmpty?: ReactNode;
}) {
    // The Transfers page is global; the floating receipt belongs to this browser.
    items = expandedPage
        ? items
        : items.filter((item) => item.initiatedHere === true);
    const collapsed =
        collapsedChoice ??
        (pageContext !== "files" ||
            Boolean(window.history.state?.tgdriveReveal) ||
            window.matchMedia("(max-width: 720px)").matches);
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
        active > 0 ||
        items.some((item) => item.state === "error" || botCleanupPending(item));
    const [floatingVisible, setFloatingVisible] = useState(unresolved);
    const attachPanel = useCallback(
        (panel: HTMLElement | null) => {
            const main = panel?.parentElement;
            if (expandedPage || !panel || !main) return;

            const syncClearance = () => {
                main.style.setProperty(
                    "--transfer-overlay-clearance",
                    panel.offsetHeight + 16 + "px",
                );
            };
            syncClearance();
            const observer = new ResizeObserver(syncClearance);
            observer.observe(panel);
            return () => {
                observer.disconnect();
                main.style.removeProperty("--transfer-overlay-clearance");
            };
        },
        [expandedPage],
    );
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
    const cancelAll = cancellable.length > 0 && (
        <button
            className="text-button"
            onClick={() => setCancelKeys(cancellable.map((item) => item.key))}
        >
            Cancel All
        </button>
    );
    const CategoryHeading = expandedPage ? "h2" : "h3";
    const cancelCount = cancelKeys
        ? batches.filter((batch) =>
              batch.items.some(
                  (item) =>
                      canCancelTransfer(item) && cancelKeys.includes(item.key),
              ),
          ).length
        : 0;
    const hasServerOperations = items.some(isNonUpload);
    const operation = hasServerOperations ? "transfer" : "upload";
    const running = batches.filter((batch) => batch.active > 0).length;
    const queued = batches.filter((batch) => batch.queued > 0).length;
    const heading = running
        ? running +
          (running === 1
              ? " " + operation + " in progress"
              : " " + operation + "s in progress")
        : queued
          ? queued +
            (queued === 1
                ? " " + operation + " waiting"
                : " " + operation + "s waiting")
          : batches.some(
                  (batch) => batch.failed || batch.awaiting || batch.cleanup,
              )
            ? hasServerOperations
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
                <Upload size={40} aria-hidden="true" />
                <h2>No Transfers Yet</h2>
                <p>
                    Start an upload or import in My Files to track its progress
                    here.
                </p>
                <button
                    type="button"
                    className="secondary-button"
                    onClick={onBrowseFiles}
                >
                    Open My Files
                </button>
            </div>
        ) : null;
    return (
        <section
            id="transfer-dialog"
            ref={attachPanel}
            className={
                "transfer-center " + (expandedPage ? "transfer-page" : "")
            }
            aria-label="Transfers"
        >
            {expandedPage && cancellable.length > 0 && (
                <p id="transfer-summary" className="sr-only">
                    {batches.length +
                        (batches.length === 1
                            ? " transfer shown"
                            : " transfers shown")}
                </p>
            )}
            {!expandedPage && (
                <header className="transfer-heading" data-compact={collapsed}>
                    {collapsed && (
                        <TransferPanelProgress batches={batches} now={now} />
                    )}
                    <h2 id="transfer-summary">
                        <button
                            id="transfer-toggle"
                            className="transfer-disclosure"
                            aria-controls="transfer-list"
                            aria-describedby={
                                collapsed ? undefined : "transfer-heading-copy"
                            }
                            aria-label={
                                collapsed
                                    ? "Show Transfer Details"
                                    : "Hide Transfer Details"
                            }
                            aria-expanded={!collapsed}
                            onClick={() => onCollapsedChange?.(!collapsed)}
                        >
                            {collapsed ? (
                                <CollapsedTransferSummary
                                    batches={batches}
                                    deviceMessage={deviceMessage}
                                />
                            ) : (
                                <span className="transfer-queue-title">
                                    <Layers size={16} aria-hidden="true" />
                                    <span id="transfer-heading-copy">
                                        {heading}
                                        {deviceMessage && (
                                            <small className="transfer-device-status">
                                                {deviceMessage}
                                            </small>
                                        )}
                                    </span>
                                </span>
                            )}
                            {collapsed ? (
                                <ChevronUp size={17} />
                            ) : (
                                <ChevronDown size={17} />
                            )}
                        </button>
                    </h2>
                </header>
            )}
            <div
                id="transfer-list"
                className="transfer-list"
                hidden={collapsed && !expandedPage}
            >
                {groups.map((group, index) => (
                    <section
                        className="transfer-category"
                        data-category={group.key}
                        key={group.key}
                        aria-label={group.label}
                    >
                        <header className="transfer-category-heading">
                            <CategoryHeading
                                className="transfer-category-title"
                                hidden={
                                    !expandedPage &&
                                    (groups.length === 1 ||
                                        (index === 0 &&
                                            (group.key === "running" ||
                                                group.key === "queued")))
                                }
                            >
                                {group.label}
                                <span>
                                    {group.key === "finished"
                                        ? (finishedCount ?? group.count)
                                        : group.count}
                                </span>
                            </CategoryHeading>
                            {expandedPage && index === 0 && (
                                <>
                                    {deviceMessage &&
                                        group.key !== "finished" && (
                                            <span
                                                className="transfer-page-guidance"
                                                role="status"
                                            >
                                                {deviceMessage}
                                            </span>
                                        )}
                                    {cancelAll}
                                </>
                            )}
                        </header>
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
                                                  onCollapsedChange?.(true);
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
            {!expandedPage && !collapsed && (
                <div className="transfer-footer">
                    {onOpenTransfers && (
                        <button
                            className="text-button transfer-open-all"
                            onClick={onOpenTransfers}
                        >
                            All transfers{" "}
                            <ChevronRight size={14} aria-hidden="true" />
                        </button>
                    )}
                    {batches.some((batch) => !batch.pending) && (
                        <button
                            id="transfer-clear"
                            className="text-button"
                            onClick={() => transfers.clearFinished()}
                            title="Remove completed and cancelled transfers from this list"
                        >
                            Clear Finished
                        </button>
                    )}
                    {cancelAll}
                </div>
            )}
            {cancelKeys !== null && (
                <Modal
                    title={
                        hasServerOperations
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
                            {hasServerOperations
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
                            {hasServerOperations
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

function isNonUpload(item: Transfer) {
    return (
        isArchiveTransfer(item) ||
        isServerImport(item) ||
        item.kind === "delete"
    );
}

function isActiveTransferStage(item: Transfer) {
    return (
        historyStatus(item) === "running" &&
        item.state !== "cancelling" &&
        !item.error
    );
}

function isWaitingTransferStage(item: Transfer) {
    return (
        [
            "retry_wait",
            "source_retry_wait",
            "waiting_for_telegram",
            "waiting_for_staging",
            "waiting_for_uploads",
            "torrent_select",
        ].includes(item.stage || "") ||
        (/^(Waiting for |Connection interrupted|Queued\b)/.test(item.phase) &&
            !item.phase.startsWith("Queued · receiving slowly"))
    );
}

function stageBytes(items: Transfer[], folder?: TransferBatch) {
    if (folder) {
        const current = folder.displayBytes;
        const total =
            (folder.device.pending ? folder.device.total : folder.totalBytes) ??
            0;
        return {
            current,
            total,
            fraction: total ? Math.min(1, current / total) : undefined,
        };
    }
    let current = 0;
    let total = 0;
    for (const item of items) {
        const bytes = transferStageProgress(item).bytes;
        if (!bytes) return;
        current += bytes.current;
        total += bytes.total;
    }
    return { current, total, fraction: total ? current / total : undefined };
}

export function MobileTransferSummary({ items }: { items: Transfer[] }) {
    const now = Date.now();
    const batches = summarizeBatches(
        items.filter((item) => item.initiatedHere),
        now,
        transfers.batchProgressSnapshot(),
    );
    return <TransferPanelProgress batches={batches} now={now} limit={1} />;
}

function PendingTransferSummary({ batch }: { batch: TransferBatch }) {
    const blocked = batch.failed || batch.awaiting || batch.cleanup;
    const Icon = blocked ? AlertCircle : Clock3;
    return (
        <span className="transfer-panel-progress-list">
            <span className="transfer-panel-progress">
                <span className="transfer-stage-symbol">
                    <Icon size={18} />
                </span>
                <span className="transfer-panel-progress-label">
                    <strong>{batch.name}</strong>
                    <span className="transfer-stage-phase">
                        {batch.awaiting
                            ? "Choose the original file to resume"
                            : blocked
                              ? "Transfer needs attention"
                              : batch.items.find((item) =>
                                    isTransferActive(item),
                                )?.phase || "Queued"}
                    </span>
                </span>
            </span>
        </span>
    );
}

function TransferPanelProgress({
    batches,
    now,
    limit = 2,
}: {
    batches: TransferBatch[];
    now: number;
    limit?: number;
}) {
    const stages = new Map<
        string,
        {
            items: Transfer[];
            batches: Set<TransferBatch>;
            waiting: boolean;
        }
    >();
    for (const batch of batches) {
        for (const item of batch.items) {
            if (!isActiveTransferStage(item)) continue;
            const waiting = isWaitingTransferStage(item);
            const meter = transferStageProgress(item);
            const phase = waiting ? item.phase : item.phase.split(" · ")[0];
            // Only counters with comparable units and denominators can share a percentage.
            const unit = waiting
                ? "waiting"
                : meter.bytes?.total
                  ? "known"
                  : meter.fraction !== undefined
                    ? item.key
                    : meter.bytes
                      ? "unknown"
                      : "unmeasured";
            const key = JSON.stringify([item.state, item.stage, phase, unit]);
            const stage = stages.get(key) ?? {
                items: [],
                batches: new Set<TransferBatch>(),
                waiting,
            };
            stage.items.push(item);
            stage.batches.add(batch);
            stages.set(key, stage);
        }
    }
    const blocked = batches.find(
        (batch) => batch.failed || batch.awaiting || batch.cleanup,
    );
    if (!stages.size || (limit === 1 && blocked)) {
        const pending = blocked ?? batches.find((batch) => batch.unfinished);
        return pending ? <PendingTransferSummary batch={pending} /> : null;
    }

    const rows = [...stages].sort(
        ([, a], [, b]) =>
            Number(a.waiting) - Number(b.waiting) ||
            Number(b.items[0].state === "receiving") -
                Number(a.items[0].state === "receiving"),
    );
    return (
        <span className="transfer-panel-progress-list">
            {rows.slice(0, limit).map(([key, { items, batches, waiting }]) => {
                const first = items[0];
                const phase =
                    items.length === 1 || waiting
                        ? first.phase
                        : first.phase.split(" · ")[0];
                const singleBatch =
                    batches.size === 1 ? [...batches][0] : undefined;
                const folder = singleBatch?.folder ? singleBatch : undefined;
                const firstMeter = transferStageProgress(first);
                const device = first.state === "receiving";
                const advancesWholeFolder =
                    folder &&
                    firstMeter.bytes?.total &&
                    (device ||
                        (!folder.device.pending &&
                            first.stage === "uploading" &&
                            folder.active === items.length &&
                            !folder.failed &&
                            !folder.awaiting &&
                            !folder.cancelled));
                const bytes = !waiting
                    ? stageBytes(
                          items,
                          advancesWholeFolder ? folder : undefined,
                      )
                    : undefined;
                const fraction = waiting
                    ? undefined
                    : (bytes?.fraction ??
                      (items.length === 1 ? firstMeter.fraction : undefined));
                const stageComplete = fraction === 1;
                const eta =
                    stageComplete || waiting
                        ? undefined
                        : advancesWholeFolder
                          ? folder.displayEta
                          : items.length === 1
                            ? transferDetails(first, now).eta
                            : undefined;
                const label = singleBatch
                    ? phase + " · " + singleBatch.name
                    : phase;
                const fileCount = folder
                    ? device
                        ? folder.device.received +
                          "/" +
                          folder.device.count +
                          " received"
                        : folder.completed + "/" + folder.count + " ready"
                    : items.length > 1
                      ? items.length +
                        (items.some(isNonUpload) ? " tasks" : " files")
                      : "";
                const percent =
                    fraction === undefined
                        ? undefined
                        : formatProgressPercent(fraction);
                const StageIcon = waiting
                    ? Clock3
                    : folder
                      ? Folder
                      : device
                        ? FileUp
                        : first.kind === "delete"
                          ? Trash2
                          : first.stage === "downloading"
                            ? Download
                            : first.kind === "archive" ||
                                first.kind === "extract"
                              ? Archive
                              : CloudUpload;
                const names = [...batches].map((batch) => batch.name);
                const title =
                    names.length === 1
                        ? names[0]
                        : names[0] + " and " + (names.length - 1) + " more";
                return (
                    <span
                        className="transfer-panel-progress"
                        key={key}
                        title={label}
                    >
                        <span className="transfer-stage-symbol">
                            {!waiting && (
                                <span
                                    className="transfer-stage-ring"
                                    role="progressbar"
                                    aria-label={
                                        stages.size === 1
                                            ? "Transfer panel progress"
                                            : "Transfer panel progress: " +
                                              phase
                                    }
                                    aria-valuenow={
                                        fraction === undefined
                                            ? undefined
                                            : fraction * 100
                                    }
                                    aria-valuetext={
                                        percent
                                            ? phase + " · " + percent
                                            : label
                                    }
                                    data-indeterminate={fraction === undefined}
                                    style={
                                        {
                                            "--sweep":
                                                fraction === undefined
                                                    ? "90deg"
                                                    : fraction * 360 + "deg",
                                        } as CSSProperties
                                    }
                                />
                            )}
                            <StageIcon size={19} aria-hidden="true" />
                        </span>
                        <span className="transfer-panel-progress-label">
                            <strong title={title}>{title}</strong>
                            {percent && (
                                <span className="transfer-panel-progress-percent">
                                    {percent}
                                </span>
                            )}
                            <span className="transfer-stage-phase">
                                {phase}
                            </span>
                        </span>
                        <span className="transfer-panel-stage-metrics">
                            {fileCount && <span>{fileCount}</span>}
                            {bytes && (bytes.total > 0 || bytes.current > 0) ? (
                                <span>
                                    {formatByteProgress(
                                        bytes.current,
                                        bytes.total,
                                    )}
                                    {bytes.total ? "" : " transferred"}
                                </span>
                            ) : null}
                            {eta !== undefined && (
                                <span data-metric="eta">
                                    About {formatDuration(Math.ceil(eta))} left
                                </span>
                            )}
                        </span>
                    </span>
                );
            })}
            {rows.length > limit && limit > 1 && (
                <span className="transfer-panel-more-stages">
                    {rows.length - limit} more{" "}
                    {rows.length - limit === 1 ? "stage" : "stages"}
                </span>
            )}
        </span>
    );
}

export function CollapsedTransferSummary({
    batches,
    deviceMessage,
}: {
    batches: TransferBatch[];
    deviceMessage: string | null;
}) {
    const hasServerOperations = batches.some((batch) =>
        batch.items.some(
            (item) => isNonUpload(item) || item.stage === "downloading",
        ),
    );
    const activeStages = batches.map((batch) =>
        batch.items.filter(isActiveTransferStage),
    );
    const uploading = activeStages.filter((items) =>
        items.some((item) => !isWaitingTransferStage(item)),
    ).length;
    const waiting = activeStages.filter(
        (items) => items.length > 0 && items.every(isWaitingTransferStage),
    ).length;
    const queued = batches.filter(
        (batch) => !batch.active && batch.queued > 0,
    ).length;
    const attention = batches.filter(
        (batch) => batch.failed || batch.awaiting || batch.cleanup,
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
            ? uploading + (hasServerOperations ? " running" : " uploading")
            : "",
        waiting ? waiting + " waiting" : "",
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
            ? hasServerOperations
                ? "Transfers complete"
                : "Uploads complete"
            : cancelled
              ? hasServerOperations
                  ? "Transfers cancelled"
                  : "Uploads cancelled"
              : batches.length
                ? hasServerOperations
                    ? "Transfers finished"
                    : "Uploads finished"
                : "Transfers");
    const StatusIcon = attention
        ? AlertCircle
        : uploading
          ? Upload
          : waiting || queued
            ? Clock3
            : cancelling || cancelled
              ? X
              : complete
                ? Check
                : Upload;
    const guidance =
        attention && !uploading && !waiting && !queued && !cancelling
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
    // Keep the browser's pending uploads in one visible phase, even when a
    // different batch has already handed its files to the server.
    const deviceStage =
        batch.device.pending ||
        (deviceUploadsPending &&
            batch.pending &&
            batch.items.some(
                (item) =>
                    !item.historyOnly &&
                    !["remote", "torrent", "delete"].includes(
                        item.kind || "",
                    ) &&
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
            batch.failed + batch.awaiting + batch.cleanup,
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
                (botCleanupPending(item) ||
                    item.state === "error" ||
                    item.state === "awaiting_file")) ||
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
            count: batch.failed + batch.awaiting + batch.cleanup,
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
                    : batch.failed || batch.awaiting || batch.cleanup
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
                            <span className="transfer-speed">
                                {formatBytes(displayRate)}/s
                            </span>
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
                            !transferBlocksRetry(item) &&
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
                                !transferBlocksRetry(item) &&
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

function TransferFilename({ name }: { name: string }) {
    const text = useRef<HTMLElement>(null);
    const [overflows, setOverflows] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const nameId = useId();
    useEffect(() => {
        const node = text.current;
        if (!node?.closest(".transfer-page")) return;
        const measure = () => {
            const style = getComputedStyle(node);
            const wraps =
                style.whiteSpace !== "nowrap" &&
                node.scrollHeight > 2 * parseFloat(style.lineHeight) + 1;
            setOverflows(wraps);
            if (!wraps) setExpanded(false);
        };
        const observer = new ResizeObserver(measure);
        observer.observe(node);
        measure();
        return () => observer.disconnect();
    }, [name]);

    return (
        <>
            <strong
                ref={text}
                id={nameId}
                className="transfer-item-name"
                title={name}
                data-expanded={expanded || undefined}
            >
                {name}
            </strong>
            {overflows && (
                <button
                    type="button"
                    className="icon-button transfer-name-toggle"
                    aria-label={
                        expanded ? "Collapse filename" : "Show full filename"
                    }
                    aria-expanded={expanded}
                    aria-controls={nameId}
                    aria-describedby={nameId}
                    onClick={() => setExpanded(!expanded)}
                >
                    {expanded ? (
                        <ChevronUp size={16} />
                    ) : (
                        <ChevronDown size={16} />
                    )}
                </button>
            )}
        </>
    );
}

function TransferRow({
    item,
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
    now: number;
    showTimestamp?: boolean;
    run: (task: Promise<unknown>) => void;
    onChooseFile: (key: string) => void;
}) {
    const [reviewOpen, setReviewOpen] = useState(false);
    const [reviewSaving, setReviewSaving] = useState(false);
    const [retryPassword, setRetryPassword] = useState<string | null>(null);
    const [retryNewPassword, setRetryNewPassword] = useState("");
    const [retrySaving, setRetrySaving] = useState(false);
    const [retryError, setRetryError] = useState("");
    const retryForm = useRef<HTMLFormElement>(null);
    useEffect(() => {
        if (retryError && !retrySaving)
            retryForm.current?.querySelector("input")?.focus();
    }, [retryError, retrySaving]);
    const retryOperation = item.pdfPasswordOperation;
    const retryMissingPassword =
        (retryOperation !== "add" && !retryPassword) ||
        (!!retryOperation && retryOperation !== "remove" && !retryNewPassword);
    const reviewJobId = item.botIntakeReviewRequired ? item.jobId : undefined;
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
    const meter = transferStageProgress(item);
    const waiting = isWaitingTransferStage(item);
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
    const liveMetrics =
        !received &&
        !finished &&
        status !== "queued" &&
        !["error", "awaiting_file"].includes(item.state);
    const showPercent =
        !received &&
        !finished &&
        status !== "queued" &&
        !(item.state === "error" && item.stage === "interrupted") &&
        !waiting &&
        meter.fraction !== undefined;
    const interruptionHelp = item.interruptedUploadRecovery
        ? {
              staged: "Retry uses the file already staged on the server.",
              finish_saving: "Retry finishes saving the existing upload.",
              original_required:
                  "The staged file is missing. Cancel this transfer, then upload the original again.",
              restore_staging:
                  "Restore the staged file on the server before retrying this upload.",
          }[item.interruptedUploadRecovery]
        : undefined;
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
                {item.state === "error" || botCleanupPending(item) ? (
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
                    <TransferFilename name={item.name} />
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
                    (received ||
                        !finished ||
                        botCleanupPending(item) ||
                        item.stage === "bot_intake_reviewed") && (
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
                        {showPercent && liveMetrics && (
                            <span className="transfer-batch-percent">
                                {formatProgressPercent(meter.fraction!)}
                            </span>
                        )}
                        {(received ||
                            finished ||
                            status === "queued" ||
                            (!waiting && meter.bytes)) && (
                            <span className="transfer-bytes">
                                {received || finished || status === "queued"
                                    ? formatBytes(item.size || item.total)
                                    : meter.bytes &&
                                      formatByteProgress(
                                          meter.bytes.current,
                                          meter.bytes.total,
                                      )}
                            </span>
                        )}
                        {!received && !finished && status !== "queued" && (
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
                {liveMetrics && !checkingSource && !waiting && (
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
                {item.stage === "torrent_select" && item.torrentChoice && (
                    <TorrentFilePicker
                        name={item.name}
                        files={item.torrentChoice}
                        onChoose={(files) =>
                            transfers.chooseTorrentFiles(item.key, files)
                        }
                    />
                )}
                {(item.state !== "cancelled" || botCleanupPending(item)) &&
                    item.stage !== "bot_intake_reviewed" &&
                    item.error && (
                        <span className="transfer-error">{item.error}</span>
                    )}
                {item.state === "error" &&
                    !item.historyOnly &&
                    interruptionHelp && (
                        <span className="transfer-device-status">
                            {interruptionHelp}
                        </span>
                    )}
                {reviewJobId && (
                    <button
                        type="button"
                        className="text-button transfer-intake-review"
                        onClick={() => setReviewOpen(true)}
                    >
                        Confirm intake channel reviewed
                    </button>
                )}
                {reviewOpen && reviewJobId && (
                    <Modal
                        title="Confirm intake channel reviewed"
                        initialFocus="[data-keep-paused]"
                        onClose={() => {
                            if (!reviewSaving) setReviewOpen(false);
                        }}
                    >
                        <p>
                            The bot queue is paused. Check the private intake
                            channel in Telegram. If you can identify the
                            temporary copy of {item.name}, remove it there.
                        </p>
                        <p>
                            Confirm review to let the bot process the next file.
                            This import stays failed.
                        </p>
                        <div className="dialog-actions">
                            <button
                                type="button"
                                className="secondary-button"
                                data-keep-paused
                                disabled={reviewSaving}
                                onClick={() => setReviewOpen(false)}
                            >
                                Keep queue paused
                            </button>
                            <button
                                type="button"
                                className="primary-button"
                                disabled={reviewSaving}
                                onClick={() => {
                                    setReviewSaving(true);
                                    run(
                                        transfers
                                            .reviewBotIntake(reviewJobId)
                                            .then(() => setReviewOpen(false))
                                            .finally(() =>
                                                setReviewSaving(false),
                                            ),
                                    );
                                }}
                            >
                                {reviewSaving
                                    ? "Confirming…"
                                    : "Confirm review"}
                            </button>
                        </div>
                    </Modal>
                )}
                {retryPassword !== null && (
                    <Modal
                        title={
                            retryOperation
                                ? {
                                      add: "Retry adding PDF password",
                                      change: "Retry changing PDF password",
                                      remove: "Retry removing PDF password",
                                  }[retryOperation]
                                : "Retry password-protected ZIP"
                        }
                        description={item.name}
                        onClose={() => !retrySaving && setRetryPassword(null)}
                    >
                        <form
                            ref={retryForm}
                            onSubmit={(event) => {
                                event.preventDefault();
                                if (retryMissingPassword || retrySaving) return;
                                setRetryError("");
                                setRetrySaving(true);
                                void transfers
                                    .retry(
                                        item.key,
                                        retryPassword || undefined,
                                        retryNewPassword || undefined,
                                    )
                                    .then(() => setRetryPassword(null))
                                    .catch((error: unknown) =>
                                        setRetryError(errorMessage(error)),
                                    )
                                    .finally(() => setRetrySaving(false));
                            }}
                        >
                            {retryOperation ? (
                                <PdfPasswordFields
                                    operation={retryOperation}
                                    password={retryPassword}
                                    setPassword={setRetryPassword}
                                    newPassword={retryNewPassword}
                                    setNewPassword={setRetryNewPassword}
                                    busy={retrySaving}
                                />
                            ) : (
                                <PasswordInput
                                    label="ZIP password"
                                    value={retryPassword}
                                    required
                                    maxLength={255}
                                    autoComplete="off"
                                    disabled={retrySaving}
                                    onChange={(event) =>
                                        setRetryPassword(
                                            event.currentTarget.value,
                                        )
                                    }
                                />
                            )}
                            <ErrorNotice message={retryError} />
                            <div className="dialog-actions">
                                <button
                                    className="secondary-button"
                                    type="button"
                                    disabled={retrySaving}
                                    onClick={() => setRetryPassword(null)}
                                >
                                    Cancel
                                </button>
                                <button
                                    className="primary-button"
                                    disabled={
                                        retryMissingPassword || retrySaving
                                    }
                                >
                                    Retry
                                </button>
                            </div>
                        </form>
                    </Modal>
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
                    !transferBlocksRetry(item) &&
                    !item.historyOnly &&
                    !needsOriginal && (
                        <button
                            className={
                                item.stage === "bot_intake_timeout"
                                    ? "text-button transfer-recovery-button"
                                    : "icon-button"
                            }
                            aria-label={"Retry " + item.name}
                            onClick={() => {
                                if (item.archivePasswordRequired) {
                                    setRetryError("");
                                    setRetryNewPassword("");
                                    setRetryPassword("");
                                } else run(transfers.retry(item.key));
                            }}
                        >
                            <RotateCcw size={17} />
                            {item.stage === "bot_intake_timeout" && "Retry"}
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

export function TransferMetrics({
    item,
    now,
}: {
    item: Transfer;
    now: number;
}) {
    const details = transferDetails(item, now);
    const waiting = isWaitingTransferStage(item);
    const retryIn =
        item.stage === "retry_wait" &&
        item.retryAt != null &&
        Number.isFinite(item.retryAt)
            ? Math.ceil(item.retryAt - now / 1000)
            : 0;
    const bytes = transferStageProgress(item).bytes;
    const remaining =
        bytes && !waiting ? remainingBytesHint(bytes.current, bytes.total) : "";
    return (
        <span className="transfer-metrics" data-timing={details.timing}>
            {remaining && <span>{remaining}</span>}
            {details.rate && !waiting ? (
                <span
                    className="transfer-speed"
                    title="Speed during this stage"
                >
                    {formatBytes(details.rate)}/s
                </span>
            ) : null}
            {details.eta !== undefined && !waiting && (
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
            {details.timing === "waiting" && retryIn > 0 && (
                <span className="transfer-timing-state">
                    Retry in {formatDuration(retryIn)}
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
