import { useEffect, useMemo, useRef, useState } from "react";
import { FolderUp, RefreshCw } from "lucide-react";
import { api, errorMessage } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import { transfers } from "./transfers";
import {
    formatBytes,
    itemPath,
    type DriveItem,
    type FolderTreeRequest,
} from "../../shared/types";
import "./FolderUpload.css";

export const MAX_FOLDER_FILES = 1000;
const MAX_FOLDERS = 1000;
const MAX_PATH_SEGMENTS = 64;
type FolderFile = { file: File; relativePath: string; parent: string };
export interface FolderUploadPlan {
    rootName: string;
    folders: string[][];
    files: FolderFile[];
    skipped: { path: string; reason: string }[];
    totalSize: number;
}
function segment(value: string): string {
    const normalized = value.normalize("NFC");
    if (
        !normalized ||
        normalized !== normalized.trim() ||
        normalized === "." ||
        normalized === ".." ||
        /[/\\\p{Cc}\p{Cs}]/u.test(normalized) ||
        [...normalized].length > 255
    )
        throw new Error(
            "A folder or file has an unsupported name. Rename it on your device and choose the folder again.",
        );
    return normalized;
}
export function planFolderUpload(
    files: File[],
    maxSize: number,
): FolderUploadPlan {
    if (!files.length)
        throw new Error(
            "The selected folder has no files this browser can provide.",
        );
    if (files.length > MAX_FOLDER_FILES)
        throw new Error("Choose a folder containing at most 1,000 files.");
    const plan: FolderUploadPlan = {
        rootName: "",
        folders: [],
        files: [],
        skipped: [],
        totalSize: 0,
    };
    const folders = new Map<string, string[]>();
    const names = new Map<string, { path: string; kind: "file" | "folder" }>();
    const reserve = (path: string, kind: "file" | "folder") => {
        const key = path.toLowerCase();
        const existing = names.get(key);
        if (
            existing &&
            (kind === "file" ||
                existing.path !== path ||
                existing.kind !== kind)
        )
            throw new Error(
                "This folder contains duplicate or conflicting names. Rename them on your device before uploading.",
            );
        names.set(key, { path, kind });
    };
    for (const file of files) {
        const relative = file.webkitRelativePath;
        if (!relative || !relative.includes("/"))
            throw new Error(
                "This browser did not provide folder paths. Use Upload Files instead.",
            );
        const parts = relative.split("/").map(segment);
        if (parts.length > MAX_PATH_SEGMENTS)
            throw new Error(
                "Folder paths can contain at most 64 levels, including the file.",
            );
        if (segment(file.name) !== parts.at(-1))
            throw new Error(
                "A selected file has an invalid folder path. Choose the folder again.",
            );
        if (!plan.rootName) plan.rootName = parts[0];
        if (parts[0] !== plan.rootName)
            throw new Error("Choose one folder at a time.");
        const path = parts.join("/");
        if (
            !Number.isSafeInteger(file.size) ||
            file.size <= 0 ||
            file.size > maxSize
        ) {
            plan.skipped.push({
                path,
                reason:
                    file.size > maxSize
                        ? "Exceeds the per-file upload limit"
                        : "Empty files are not supported",
            });
            continue;
        }
        for (let index = 1; index < parts.length - 1; index++) {
            const folder = parts.slice(1, index + 1);
            const key = folder.join("/");
            reserve(key, "folder");
            folders.set(key, folder);
        }
        const withoutRoot = parts.slice(1).join("/");
        reserve(withoutRoot, "file");
        plan.files.push({
            file,
            relativePath: path,
            parent: parts.slice(1, -1).join("/"),
        });
        plan.totalSize += file.size;
        if (!Number.isSafeInteger(plan.totalSize))
            throw new Error(
                "This selection is too large to track safely. Choose a smaller folder.",
            );
    }
    if (!plan.files.length)
        throw new Error(
            "No supported files were found. Empty files and files above the upload limit cannot be uploaded.",
        );
    if (folders.size + 1 > MAX_FOLDERS)
        throw new Error("Choose a folder containing at most 1,000 folders.");
    plan.folders = [...folders.values()].sort(
        (a, b) => a.length - b.length || a.join("/").localeCompare(b.join("/")),
    );
    return plan;
}
export function folderTreeRequest(
    plan: FolderUploadPlan,
    destination: string,
    requestId: string,
): FolderTreeRequest {
    const payload = {
        destination,
        root_name: plan.rootName,
        folders: plan.folders,
        client_request_id: requestId,
    };
    if (new TextEncoder().encode(JSON.stringify(payload)).length > 64 * 1024)
        throw new Error(
            "The folder names exceed the preparation limit. Choose a smaller folder.",
        );
    return payload;
}
export async function prepareFolderUpload(
    plan: FolderUploadPlan,
    destination: string,
    requestId: string,
    signal: AbortSignal,
) {
    const result = await api.prepareFolderTree(
        folderTreeRequest(plan, destination, requestId),
        signal,
    );
    const rootPath = result.folders?.[""];
    if (
        !result.root ||
        result.root.type !== "folder" ||
        typeof result.root.id !== "string" ||
        typeof result.root.path !== "string" ||
        typeof rootPath !== "string" ||
        !rootPath.startsWith("/") ||
        !result.root.id ||
        typeof result.root.name !== "string" ||
        !rootPath.startsWith(
            (destination === "/" ? "" : destination.replace(/\/$/, "")) + "/",
        ) ||
        rootPath
            .slice(
                (destination === "/" ? "" : destination.replace(/\/$/, ""))
                    .length + 1,
            )
            .includes("/") ||
        rootPath === "/" ||
        itemPath(result.root) !== rootPath
    )
        throw new Error(
            "The server returned invalid folder preparation information. Retry Preparation.",
        );
    for (const parts of plan.folders) {
        const path = result.folders[parts.join("/")];
        const parent = result.folders[parts.slice(0, -1).join("/")];
        if (
            typeof path !== "string" ||
            typeof parent !== "string" ||
            !path.startsWith(parent + "/") ||
            path.slice(parent.length + 1).includes("/") ||
            path === parent + "/"
        )
            throw new Error(
                "The server returned an incomplete folder tree. Retry Preparation.",
            );
    }
    return {
        root: result.root,
        entries: plan.files.map((entry) => ({
            file: entry.file,
            destination: result.folders[entry.parent],
            relativePath: entry.relativePath,
        })),
    };
}

export default function FolderUpload({
    files,
    destination,
    destinationName,
    maxSize,
    supported,
    onClose,
    onQueued,
    onFilesFallback,
    onChanged,
}: {
    files: File[];
    destination: string;
    destinationName: string;
    maxSize: number;
    supported: boolean;
    onClose: () => void;
    onQueued: (root: DriveItem, count: number) => void;
    onFilesFallback: () => void;
    onChanged: () => void;
}) {
    const planned = useMemo(() => {
        if (!supported) return { plan: null, error: "" };
        try {
            return { plan: planFolderUpload(files, maxSize), error: "" };
        } catch (error) {
            return { plan: null, error: errorMessage(error) };
        }
    }, [files, maxSize, supported]);
    const [requestId] = useState(() => crypto.randomUUID());
    const [state, setState] = useState<
        "review" | "preparing" | "cancelled" | "error"
    >("review");
    const [error, setError] = useState("");
    const controller = useRef<AbortController | null>(null);
    const prepared = useRef<Awaited<
        ReturnType<typeof prepareFolderUpload>
    > | null>(null);
    const mounted = useRef(true);
    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            controller.current?.abort();
        };
    }, []);
    const close = () => {
        if (state === "preparing") {
            controller.current?.abort();
            return;
        }
        onClose();
    };
    const start = async () => {
        const plan = planned.plan;
        if (!plan || state === "preparing") return;
        if (!transfers.canQueue(plan.files.length)) {
            setError(
                "The upload queue is full. Wait for existing device transfers or cancel some, then retry.",
            );
            setState("error");
            return;
        }
        const active = new AbortController();
        controller.current = active;
        setState("preparing");
        setError("");
        try {
            const result =
                prepared.current ||
                (await prepareFolderUpload(
                    plan,
                    destination,
                    requestId,
                    active.signal,
                ));
            prepared.current = result;
            if (active.signal.aborted || !mounted.current) return;
            const rejected = transfers.addEntries(
                result.entries,
                maxSize,
                true,
                {
                    name: result.root.name,
                    folderUpload: {
                        id: requestId,
                        name: result.root.name,
                        root_path: itemPath(result.root),
                        created_at: Date.now() / 1000,
                    },
                },
            );
            if (rejected.length) throw new Error(rejected.join("; "));
            onChanged();
            onQueued(result.root, result.entries.length);
        } catch (error) {
            if (!mounted.current) return;
            if (active.signal.aborted) setState("cancelled");
            else {
                setState("error");
                setError(errorMessage(error));
            }
        } finally {
            if (mounted.current && active.signal.aborted) setState("cancelled");
            if (controller.current === active) controller.current = null;
            onChanged();
        }
    };
    const fallback = () => {
        onClose();
        onFilesFallback();
    };
    if (!supported)
        return (
            <Modal title="Upload Folder" onClose={onClose}>
                <p>
                    Folder selection is not available in this browser. You can
                    upload files instead, or use a browser that supports
                    choosing folders.
                </p>
                <div className="dialog-actions">
                    <button className="secondary-button" onClick={onClose}>
                        Close
                    </button>
                    <button className="primary-button" onClick={fallback}>
                        Upload Files
                    </button>
                </div>
            </Modal>
        );
    const plan = planned.plan;
    return (
        <Modal title="Upload Folder" onClose={close}>
            <ErrorNotice message={planned.error || error} />
            {plan && (
                <>
                    <div className="folder-upload-summary">
                        <FolderUp size={27} aria-hidden="true" />
                        <div>
                            <strong>{plan.rootName}</strong>
                            <span>
                                {plan.files.length.toLocaleString()}{" "}
                                {plan.files.length === 1 ? "file" : "files"} ·{" "}
                                {formatBytes(plan.totalSize)}
                            </span>
                        </div>
                    </div>
                    <p className="folder-upload-destination">
                        To <strong>{destinationName}</strong>
                    </p>
                    {plan.skipped.length > 0 && (
                        <details className="folder-upload-skipped" open>
                            <summary>
                                {plan.skipped.length}{" "}
                                {plan.skipped.length === 1 ? "file" : "files"}{" "}
                                will be skipped
                            </summary>
                            <ul>
                                {plan.skipped
                                    .slice(0, 20)
                                    .map((entry, index) => (
                                        <li key={index}>
                                            <span>{entry.path}</span> —{" "}
                                            {entry.reason}
                                        </li>
                                    ))}
                            </ul>
                            {plan.skipped.length > 20 && (
                                <p>
                                    And {plan.skipped.length - 20} more
                                    unsupported files.
                                </p>
                            )}
                        </details>
                    )}
                    {state === "preparing" && (
                        <div className="folder-upload-progress" role="status">
                            <Spinner />
                            <strong>Preparing folders…</strong>
                        </div>
                    )}
                    {state === "cancelled" && (
                        <p role="status" className="folder-upload-note">
                            Preparation cancelled. No files were queued. An
                            empty folder may remain.
                        </p>
                    )}
                    {state === "error" && (
                        <p className="folder-upload-note">
                            No files were queued. Retry Preparation, or close
                            this dialog to choose the folder again.
                        </p>
                    )}
                </>
            )}
            <div className="dialog-actions">
                <button className="secondary-button" onClick={close}>
                    {state === "preparing" ? "Cancel Preparation" : "Close"}
                </button>
                {plan ? (
                    <button
                        className="primary-button"
                        disabled={state === "preparing"}
                        onClick={() => void start()}
                    >
                        {state === "preparing" ? (
                            <Spinner />
                        ) : state === "review" ? (
                            <FolderUp size={17} />
                        ) : (
                            <RefreshCw size={17} />
                        )}
                        {state === "review"
                            ? "Upload"
                            : state === "preparing"
                              ? "Preparing folders…"
                              : "Retry Preparation"}
                    </button>
                ) : (
                    <button className="primary-button" onClick={fallback}>
                        Upload Files
                    </button>
                )}
            </div>
        </Modal>
    );
}
