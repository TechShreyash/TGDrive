import { useState } from "react";
import { Modal } from "../../shared/components";
import { formatBytes } from "../../shared/types";

/** Indexes and byte total of the chosen files. */
export function torrentSelection(
    files: { size: number }[],
    chosen: ReadonlySet<number>,
) {
    const indexes = [...chosen].filter((i) => i >= 0 && i < files.length);
    indexes.sort((a, b) => a - b);
    return {
        indexes,
        size: indexes.reduce((total, i) => total + files[i].size, 0),
    };
}

/** Row action that opens the file choice in a full-size dialog. */
export default function TorrentFilePicker({
    name,
    files,
    onChoose,
}: {
    name: string;
    files: { path: string; size: number }[];
    onChoose: (indexes: number[]) => Promise<void>;
}) {
    const [open, setOpen] = useState(false);
    return (
        <>
            <button
                type="button"
                className="secondary-button torrent-choose-button"
                onClick={() => setOpen(true)}
            >
                Choose files ({files.length})
            </button>
            {open && (
                <Modal
                    title={"Choose files from " + name}
                    wide
                    onClose={() => setOpen(false)}
                >
                    <TorrentFileChoice
                        name={name}
                        files={files}
                        onChoose={async (indexes) => {
                            await onChoose(indexes);
                            setOpen(false);
                        }}
                    />
                </Modal>
            )}
        </>
    );
}

function TorrentFileChoice({
    name,
    files,
    onChoose,
}: {
    name: string;
    files: { path: string; size: number }[];
    onChoose: (indexes: number[]) => Promise<void>;
}) {
    const [chosen, setChosen] = useState(
        () => new Set(files.map((_, index) => index)),
    );
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const { indexes, size } = torrentSelection(files, chosen);
    const prefix = name + "/";
    const toggle = (index: number) =>
        setChosen((current) => {
            const next = new Set(current);
            if (!next.delete(index)) next.add(index);
            return next;
        });
    return (
        <form
            className="torrent-picker"
            aria-label={"Choose files from " + name}
            onSubmit={(event) => {
                event.preventDefault();
                setBusy(true);
                setError("");
                onChoose(indexes)
                    .catch((cause) => setError(String(cause?.message || cause)))
                    .finally(() => setBusy(false));
            }}
        >
            <p className="settings-help">
                Only the files you keep selected are downloaded and saved.
            </p>
            <div className="torrent-picker-actions">
                <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                        setChosen(new Set(files.map((_, index) => index)))
                    }
                >
                    Select all
                </button>
                <button
                    type="button"
                    className="text-button"
                    onClick={() => setChosen(new Set())}
                >
                    Select none
                </button>
                <span className="torrent-picker-count">
                    {indexes.length} of {files.length} selected
                </span>
            </div>
            <ul className="torrent-picker-files">
                {files.map((file, index) => (
                    <li key={file.path}>
                        <label>
                            <input
                                type="checkbox"
                                checked={chosen.has(index)}
                                onChange={() => toggle(index)}
                                disabled={busy}
                            />
                            <span className="torrent-picker-path">
                                {file.path.startsWith(prefix)
                                    ? file.path.slice(prefix.length)
                                    : file.path}
                            </span>
                            <span className="torrent-picker-size">
                                {formatBytes(file.size)}
                            </span>
                        </label>
                    </li>
                ))}
            </ul>
            {error && <p role="alert">{error}</p>}
            <div className="torrent-picker-submit">
                <button
                    type="submit"
                    className="primary-button"
                    disabled={busy || !indexes.length}
                >
                    {busy
                        ? "Starting…"
                        : "Import " +
                          indexes.length +
                          (indexes.length === 1 ? " file" : " files") +
                          " · " +
                          formatBytes(size)}
                </button>
            </div>
        </form>
    );
}
