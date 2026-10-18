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
  Archive,
} from "lucide-react";
export function Spinner() {
  return <LoaderCircle className="spinner" aria-hidden="true" size={18} />;
}
export function FileIcon({
  name,
  folder = false,
}: {
  name: string;
  folder?: boolean;
}) {
  const extension = name.split(".").at(-1)?.toLowerCase() || "";
  const Icon = folder
    ? Folder
    : ["mp4", "mkv", "webm", "mov", "avi"].includes(extension)
      ? Film
      : ["png", "jpg", "jpeg", "gif", "webp", "avif", "heic", "svg"].includes(
            extension,
          )
        ? Image
        : ["mp3", "wav", "ogg", "flac", "m4a"].includes(extension)
          ? Music
          : ["zip", "gz", "7z", "rar", "tar"].includes(extension)
            ? Archive
            : ["txt", "md", "pdf", "doc", "docx"].includes(extension)
              ? FileText
              : File;
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
    node
      .querySelector<HTMLElement>(
        initialFocus || "input:not([type=checkbox]), select, textarea",
      )
      ?.focus();
    return () => {
      node.close();
      if (trigger?.isConnected && trigger !== document.body) trigger.focus();
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
    <p className="error-notice" role="alert">
      {message}
    </p>
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
  const closeNotice = useRef(close);
  closeNotice.current = close;
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
      if (modal() !== anchor || !anchor.isConnected) closeNotice.current();
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
    // Closing a dialog can detach its portal before the observer relocates it.
    // The next container render raises the same notice without stealing focus.
    if (!node?.isConnected || typeof node.showPopover !== "function") return;
    node.showPopover();
    return () => {
      if (node.isConnected && node.matches(":popover-open")) node.hidePopover();
    };
  }, [container, toast.message, toast.error]);
  return createPortal(
    <div
      ref={notice}
      popover="manual"
      className={"toast " + (toast.error ? "toast-error" : "")}
      role={toast.error ? "alert" : "status"}
      aria-live={toast.error ? "assertive" : "polite"}
    >
      <span>{toast.message}</span>
      <button
        className="icon-button"
        aria-label="Dismiss Notification"
        onClick={close}
      >
        <X size={17} />
      </button>
    </div>,
    container,
  );
}
