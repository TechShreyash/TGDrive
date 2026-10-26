import { Alert } from "@mantine/core";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
    X,
    LoaderCircle,
    Folder,
    File,
    FileText,
    Film,
    Image,
    Music,
    FileArchive,
    FileSpreadsheet,
    Presentation,
    BookOpen,
    AlertCircle,
} from "lucide-react";
export function Spinner() {
    return <LoaderCircle className="spinner" aria-hidden="true" size={18} />;
}

const FILE_ICON_GROUPS = [
    { icon: Film, extensions: ["mp4", "mkv", "webm", "mov", "avi"] },
    {
        icon: Image,
        extensions: [
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "avif",
            "heic",
            "svg",
        ],
    },
    { icon: Music, extensions: ["mp3", "wav", "ogg", "flac", "m4a"] },
    { icon: FileArchive, extensions: ["zip", "gz", "7z", "rar", "tar"] },
    { icon: FileSpreadsheet, extensions: ["csv", "xlsx", "xls", "ods"] },
    { icon: Presentation, extensions: ["ppt", "pptx", "odp"] },
    { icon: BookOpen, extensions: ["epub", "cbz", "cbr"] },
    { icon: FileText, extensions: ["txt", "md", "pdf", "doc", "docx"] },
];

function iconForExtension(extension: string) {
    for (const group of FILE_ICON_GROUPS) {
        if (group.extensions.includes(extension)) return group.icon;
    }
    return File;
}

export function FileIcon({
    name,
    folder = false,
}: {
    name: string;
    folder?: boolean;
}) {
    const extension = name.split(".").at(-1)?.toLowerCase() || "";
    const Icon = folder ? Folder : iconForExtension(extension);
    return (
        <span
            className={"file-icon " + (folder ? "folder-icon" : "")}
            data-extension={extension}
        >
            <Icon size={22} aria-hidden="true" />
        </span>
    );
}
export function Modal({
    title,
    children,
    onClose,
    wide = false,
    initialFocus,
}: {
    title: string;
    children: ReactNode;
    onClose: () => void;
    wide?: boolean;
    initialFocus?: string;
}) {
    const dialog = useRef<HTMLDialogElement>(null);
    const titleId = useId();
    useEffect(() => {
        const node = dialog.current!;
        const trigger = document.activeElement as HTMLElement | null;
        node.showModal();
        const cycleFocus = (event: KeyboardEvent) => {
            if (event.key !== "Tab") return;
            const controls = Array.from(
                node.querySelectorAll<HTMLElement>(
                    "a[href], button, input, select, textarea, summary, [tabindex]",
                ),
            ).filter(
                (node) =>
                    node.tabIndex >= 0 &&
                    !node.matches(":disabled") &&
                    !node.closest("[inert]") &&
                    node.getClientRects().length > 0 &&
                    getComputedStyle(node).visibility !== "hidden",
            );
            const first = controls[0];
            const last = controls.at(-1);
            if (!first || !last) {
                event.preventDefault();
                return;
            }
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        // Use a DOM listener so notifications portaled into this dialog participate.
        node.addEventListener("keydown", cycleFocus);
        node.querySelector<HTMLElement>(
            initialFocus || "input:not([type=checkbox]), select, textarea",
        )?.focus();
        return () => {
            node.removeEventListener("keydown", cycleFocus);
            node.close();
            if (trigger?.isConnected && trigger !== document.body)
                trigger.focus();
            else document.getElementById("main-content")?.focus();
        };
    }, [initialFocus]);
    return (
        <dialog
            ref={dialog}
            className={"modal " + (wide ? "modal-wide" : "")}
            onCancel={(event) => {
                event.preventDefault();
                onClose();
            }}
            onClick={(event) => {
                if (event.target === event.currentTarget) {
                    const rect = event.currentTarget.getBoundingClientRect();
                    if (
                        event.clientX < rect.left ||
                        event.clientX > rect.right ||
                        event.clientY < rect.top ||
                        event.clientY > rect.bottom
                    )
                        onClose();
                }
            }}
            aria-labelledby={titleId}
        >
            <header className="modal-header">
                <h2 id={titleId}>{title}</h2>
                <button
                    className="icon-button"
                    type="button"
                    aria-label="Close Dialog"
                    onClick={onClose}
                >
                    <X size={20} />
                </button>
            </header>
            {children}
        </dialog>
    );
}
export function ErrorNotice({ message }: { message: string }) {
    return message ? (
        <Alert
            className="error-notice"
            color="red"
            variant="light"
            icon={<AlertCircle size={18} aria-hidden="true" />}
            role="alert"
        >
            {message}
        </Alert>
    ) : null;
}

/** Native modal dialogs live above CSS z-index. Keep notices in their focus tree
 * and raise a manual popover without moving the user's keyboard focus. */
export function ToastNotice({
    toast,
    close,
}: {
    toast: { message: string; error: boolean };
    close: () => void;
}) {
    const [container, setContainer] = useState<HTMLElement>(document.body);
    const notice = useRef<HTMLDivElement>(null);
    const returnFocus = useRef<HTMLElement | null>(null);
    useEffect(() => {
        if (!notice.current?.contains(document.activeElement))
            returnFocus.current = document.activeElement as HTMLElement | null;
    }, [toast]);
    const closeNotice = useRef(close);
    closeNotice.current = close;
    const [hovered, setHovered] = useState(false);
    const [focused, setFocused] = useState(false);
    useEffect(() => {
        if (toast.error || hovered || focused) return;
        const timer = window.setTimeout(() => closeNotice.current(), 6000);
        return () => window.clearTimeout(timer);
    }, [toast, hovered, focused]);
    useEffect(() => {
        const modal = () => {
            const dialogs =
                document.querySelectorAll<HTMLDialogElement>("dialog[open]");
            return dialogs[dialogs.length - 1] || document.body;
        };
        const anchor = modal();
        setContainer(anchor);
        const observer = new MutationObserver(() => {
            // A notice belongs to the action that created it. Do not reannounce old
            // feedback inside a different task's newly opened modal.
            if (modal() !== anchor || !anchor.isConnected)
                closeNotice.current();
        });
        observer.observe(document.body, {
            subtree: true,
            childList: true,
            attributes: true,
            attributeFilter: ["open"],
        });
        return () => observer.disconnect();
    }, [toast.message, toast.error]);
    useEffect(() => {
        const node = notice.current;
        // Portals move into the current modal before entering the top layer.
        if (!node?.isConnected || typeof node.showPopover !== "function")
            return;
        node.showPopover();
        return () => {
            if (node.isConnected && node.matches(":popover-open"))
                node.hidePopover();
        };
    }, [container, toast.message, toast.error]);
    return createPortal(
        <div
            ref={notice}
            popover="manual"
            className={"toast " + (toast.error ? "toast-error" : "")}
            role={toast.error ? "alert" : "status"}
            aria-live={toast.error ? "assertive" : "polite"}
            onMouseEnter={() => setHovered(true)}
            onMouseLeave={() => setHovered(false)}
            onFocusCapture={() => setFocused(true)}
            onBlurCapture={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget))
                    setFocused(false);
            }}
        >
            <span tabIndex={toast.error ? 0 : undefined}>{toast.message}</span>
            <button
                className="icon-button"
                type="button"
                aria-label="Dismiss Notification"
                onClick={() => {
                    const restore = notice.current?.contains(
                        document.activeElement,
                    );
                    close();
                    if (restore) {
                        const target = returnFocus.current;
                        if (target?.isConnected && target !== document.body)
                            target.focus();
                        else document.getElementById("main-content")?.focus();
                    }
                }}
            >
                <X size={17} />
            </button>
        </div>,
        container,
    );
}
