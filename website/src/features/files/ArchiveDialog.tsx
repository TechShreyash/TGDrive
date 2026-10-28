import { useState } from "react";
import { Archive, FolderArchive } from "lucide-react";
import { ApiError, errorMessage } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import { itemPath, type DriveItem } from "../../shared/types";
import { transfers } from "../transfers/transfers";
import {
    archiveApi,
    defaultArchiveName,
    extractionFolderName,
} from "./archiveApi";
import "./ArchiveDialog.css";

export type ArchiveDialogState =
    | { type: "archive"; items: DriveItem[] }
    | { type: "extract"; item: DriveItem };

export default function ArchiveDialog({
    dialog,
    destination,
    onClose,
    onNotice,
}: {
    dialog: ArchiveDialogState;
    destination: string;
    onClose: () => void;
    onNotice: (message: string, error?: boolean) => void;
}) {
    const extracting = dialog.type === "extract";
    const items = extracting ? [dialog.item] : dialog.items;
    const parent = items[0].path || "/";
    const target = items.every((item) => (item.path || "/") === parent)
        ? parent
        : destination;
    const targetLabel =
        target === "/"
            ? "My Files"
            : target === parent && items[0].parent_names?.length
              ? ["My Files", ...(items[0].parent_names || [])].join(" / ")
              : "Current folder";
    const [name, setName] = useState(() =>
        extracting
            ? extractionFolderName(dialog.item.name)
            : defaultArchiveName(dialog.items),
    );
    const [newFolder, setNewFolder] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [exposure, setExposure] = useState(false);
    const [allow, setAllow] = useState(false);
    const close = () => {
        if (!busy) onClose();
    };
    const submit = async () => {
        if (busy) return;
        setBusy(true);
        setError("");
        try {
            const result = extracting
                ? await archiveApi.extract(
                      itemPath(dialog.item),
                      target,
                      newFolder ? name.trim() : null,
                      allow,
                  )
                : await archiveApi.create(
                      dialog.items.map(itemPath),
                      target,
                      name.trim(),
                      allow,
                  );
            transfers.addJob(
                result.id,
                result.job.name || name.trim(),
                result.job.destination || target,
                dialog.type,
            );
            onNotice(
                extracting
                    ? "Extraction queued. Track progress in Transfers. You can close this tab."
                    : "ZIP creation queued. Track progress in Transfers, then download the ZIP from your drive. You can close this tab.",
            );
            onClose();
        } catch (cause) {
            if (
                cause instanceof ApiError &&
                cause.code.includes("PUBLIC_EXPOSURE")
            ) {
                setExposure(true);
                setError(
                    "This folder has a public link. Confirm below to make the output accessible through that link.",
                );
            } else setError(errorMessage(cause));
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal
            title={extracting ? "Extract archive" : "Create ZIP"}
            initialFocus="#archive-name"
            onClose={close}
        >
            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    void submit();
                }}
            >
                <p className="archive-source">
                    {items.length === 1
                        ? items[0].name
                        : items.length + " selected files and folders"}
                </p>
                <p>
                    {extracting
                        ? "Extract the archive’s files and folders into your drive. The original is kept."
                        : "Save one ZIP in your drive to download these items together. Original files and folders are kept."}
                </p>
                <p className="archive-location">
                    {extracting ? "Extract in: " : "Save in: "}
                    <strong>{targetLabel}</strong>
                </p>
                {extracting && (
                    <fieldset className="archive-destination-options">
                        <legend>Destination</legend>
                        <label className="check-field">
                            <input
                                type="radio"
                                name="archive-destination"
                                value="new"
                                checked={newFolder}
                                disabled={busy}
                                onChange={() => setNewFolder(true)}
                            />
                            <span>New folder</span>
                        </label>
                        <label className="check-field">
                            <input
                                type="radio"
                                name="archive-destination"
                                value="current"
                                checked={!newFolder}
                                disabled={busy}
                                onChange={() => setNewFolder(false)}
                            />
                            <span>This folder</span>
                        </label>
                    </fieldset>
                )}
                {(!extracting || newFolder) && (
                    <>
                        <label htmlFor="archive-name">
                            {extracting ? "New folder name" : "ZIP name"}
                        </label>
                        <input
                            id="archive-name"
                            value={name}
                            maxLength={255}
                            required
                            disabled={busy}
                            aria-describedby="archive-hint"
                            onChange={(event) => setName(event.target.value)}
                        />
                    </>
                )}
                <p id="archive-hint" className="muted">
                    Existing items are never overwritten. Matching names get a
                    numbered copy. Progress and failures appear in Transfers;
                    work continues on the server after you close this tab.
                </p>
                {extracting && (
                    <>
                        <p className="muted">
                            Supports ZIP, 7z, RAR, TAR (including compressed
                            TAR), GZip, BZip2, XZ and Zstandard.
                        </p>
                        <p className="muted">
                            Password-protected or split archives, and archives
                            containing empty files, are not supported. Empty
                            folders are kept.
                        </p>
                    </>
                )}
                <ErrorNotice message={error} />
                {exposure && (
                    <label className="check-field">
                        <input
                            type="checkbox"
                            checked={allow}
                            disabled={busy}
                            onChange={(event) => setAllow(event.target.checked)}
                        />
                        <span>
                            I understand that files created here may become
                            public.
                        </span>
                    </label>
                )}
                <div className="dialog-actions">
                    <button
                        className="secondary-button"
                        type="button"
                        onClick={close}
                        disabled={busy}
                    >
                        Cancel
                    </button>
                    <button
                        className="primary-button"
                        disabled={
                            busy ||
                            ((!extracting || newFolder) && !name.trim()) ||
                            (exposure && !allow)
                        }
                    >
                        {busy ? (
                            <Spinner />
                        ) : extracting ? (
                            <FolderArchive size={16} aria-hidden="true" />
                        ) : (
                            <Archive size={16} aria-hidden="true" />
                        )}
                        {extracting ? "Extract archive" : "Create ZIP"}
                    </button>
                </div>
            </form>
        </Modal>
    );
}
