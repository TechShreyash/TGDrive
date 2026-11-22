import { useEffect, useRef, useState } from "react";
import { File, Folder, LockKeyhole, RefreshCw } from "lucide-react";
import { errorMessage, sessionExpiryHandler } from "../../../shared/api";
import { ErrorNotice, Spinner } from "../../../shared/components";
import { formatBytes } from "../../../shared/types";
import { readBoundedResponse } from "../previewIO";
import "./ArchivePreview.css";

export interface ArchiveEntry {
    name: string;
    directory: boolean;
    size: number;
    encrypted: boolean;
}

export function archiveEntries(value: unknown): ArchiveEntry[] {
    const data = value as { kind?: unknown; entries?: unknown } | null;
    if (
        data?.kind !== "archive" ||
        !Array.isArray(data.entries) ||
        data.entries.length > 10000
    )
        throw new Error("This ZIP directory could not be read.");
    for (const entry of data.entries) {
        if (
            !entry ||
            typeof entry.name !== "string" ||
            !entry.name ||
            entry.name.length > 1024 ||
            entry.name.startsWith("/") ||
            entry.name.includes("//") ||
            /[\\\x00-\x1f]/.test(entry.name) ||
            entry.name
                .split("/")
                .some((part: string) => part === "." || part === "..") ||
            typeof entry.directory !== "boolean" ||
            typeof entry.encrypted !== "boolean" ||
            !Number.isSafeInteger(entry.size) ||
            entry.size < 0
        )
            throw new Error("This ZIP contains an invalid directory entry.");
    }
    return data.entries as ArchiveEntry[];
}

export function archiveFolder(
    entries: ArchiveEntry[],
    prefix: string,
): ArchiveEntry[] {
    const folders = new Map<string, ArchiveEntry>();
    const files: ArchiveEntry[] = [];
    for (const entry of entries) {
        if (!entry.name.startsWith(prefix)) continue;
        const relative = entry.name.slice(prefix.length);
        if (!relative) continue;
        const slash = relative.indexOf("/");
        if (slash >= 0) {
            const name = relative.slice(0, slash);
            if (name)
                folders.set(name, {
                    name,
                    directory: true,
                    size: 0,
                    encrypted: false,
                });
        } else if (entry.directory)
            folders.set(relative, { ...entry, name: relative });
        else files.push({ ...entry, name: relative });
    }
    const byName = (a: ArchiveEntry, b: ArchiveEntry) =>
        a.name.localeCompare(b.name, undefined, { numeric: true });
    return [...folders.values()].sort(byName).concat(files.sort(byName));
}

export default function ArchivePreview({
    path,
    auth,
}: {
    path: string;
    auth: string;
}) {
    const [entries, setEntries] = useState<ArchiveEntry[] | null>(null);
    const [prefix, setPrefix] = useState("");
    const [page, setPage] = useState(0);
    const [attempt, setAttempt] = useState(0);
    const [error, setError] = useState("");
    const heading = useRef<HTMLHeadingElement>(null);
    useEffect(() => {
        const controller = new AbortController();
        const expire = sessionExpiryHandler();
        const params = new URLSearchParams({ path });
        if (auth) params.set("auth", auth);
        setEntries(null);
        setError("");
        void (async () => {
            const response = await fetch("/api/v1/books?" + params, {
                credentials: "same-origin",
                headers: { Accept: "application/json" },
                signal: AbortSignal.any([
                    controller.signal,
                    AbortSignal.timeout(60000),
                ]),
            });
            expire(response.status);
            const data = JSON.parse(
                new TextDecoder().decode(
                    await readBoundedResponse(
                        response,
                        8 * 1024 * 1024,
                        controller.signal,
                    ),
                ),
            );
            if (!response.ok)
                throw new Error(
                    typeof data.detail === "string"
                        ? data.detail
                        : "This ZIP could not be read.",
                );
            const result = archiveEntries(data);
            if (!controller.signal.aborted) setEntries(result);
        })().catch((cause) => {
            if (!controller.signal.aborted) setError(errorMessage(cause));
        });
        return () => controller.abort();
    }, [path, auth, attempt]);
    function navigate(next: string) {
        setPrefix(next);
        setPage(0);
        heading.current?.focus({ preventScroll: true });
    }
    if (error)
        return (
            <div className="archive-preview">
                <ErrorNotice message={error} />
                <button
                    className="secondary-button"
                    onClick={() => setAttempt(attempt + 1)}
                >
                    <RefreshCw size={16} aria-hidden="true" /> Try again
                </button>
            </div>
        );
    if (!entries)
        return (
            <div className="preview-loading" role="status">
                <Spinner /> Reading ZIP directory…
            </div>
        );
    const rows = archiveFolder(entries, prefix);
    const parts = prefix.split("/").filter(Boolean);
    return (
        <section className="archive-preview" aria-label="ZIP contents">
            <nav
                className="archive-breadcrumbs"
                aria-label="Folders inside ZIP"
            >
                <button onClick={() => navigate("")} disabled={!prefix}>
                    ZIP contents
                </button>
                {parts.map((part, index) => (
                    <span key={index}>
                        {" "}
                        /{" "}
                        <button
                            disabled={index === parts.length - 1}
                            onClick={() =>
                                navigate(
                                    parts.slice(0, index + 1).join("/") + "/",
                                )
                            }
                        >
                            {part}
                        </button>
                    </span>
                ))}
            </nav>
            <h3 ref={heading} tabIndex={-1}>
                {parts.at(-1) || "ZIP contents"}
            </h3>
            <p className="muted">
                {rows.length} {rows.length === 1 ? "item" : "items"} · Read-only
                preview. Files have not been extracted.
            </p>
            {rows.length ? (
                <table>
                    <caption className="sr-only">
                        Files and folders in {prefix || "the ZIP root"}
                    </caption>
                    <thead>
                        <tr>
                            <th scope="col">Name</th>
                            <th scope="col">Size</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows
                            .slice(page * 100, (page + 1) * 100)
                            .map((entry, index) => (
                                <tr key={index}>
                                    <td>
                                        {entry.directory ? (
                                            <button
                                                className="archive-folder"
                                                onClick={() =>
                                                    navigate(
                                                        prefix +
                                                            entry.name +
                                                            "/",
                                                    )
                                                }
                                            >
                                                <Folder
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                                <span>{entry.name}</span>
                                            </button>
                                        ) : (
                                            <span className="archive-file">
                                                <File
                                                    size={18}
                                                    aria-hidden="true"
                                                />
                                                <span>{entry.name}</span>
                                                {entry.encrypted && (
                                                    <span className="archive-encrypted">
                                                        <LockKeyhole
                                                            size={14}
                                                            aria-hidden="true"
                                                        />{" "}
                                                        Password protected
                                                    </span>
                                                )}
                                            </span>
                                        )}
                                    </td>
                                    <td>
                                        {entry.directory
                                            ? "—"
                                            : formatBytes(entry.size)}
                                    </td>
                                </tr>
                            ))}
                    </tbody>
                </table>
            ) : (
                <p>This folder is empty.</p>
            )}
            {rows.length > 100 && (
                <div className="archive-pagination">
                    <button
                        className="secondary-button"
                        disabled={!page}
                        onClick={() => setPage(page - 1)}
                    >
                        Previous
                    </button>
                    <span role="status">
                        Page {page + 1} of {Math.ceil(rows.length / 100)}
                    </span>
                    <button
                        className="secondary-button"
                        disabled={(page + 1) * 100 >= rows.length}
                        onClick={() => setPage(page + 1)}
                    >
                        Next
                    </button>
                </div>
            )}
        </section>
    );
}
