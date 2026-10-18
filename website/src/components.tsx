import { useEffect, useId, useRef, type ReactNode } from "react";
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
