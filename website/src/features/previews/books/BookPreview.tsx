import {
    createElement,
    useEffect,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
    ChevronLeft,
    ChevronRight,
    Minus,
    Plus,
    RefreshCw,
    Settings2,
} from "lucide-react";
import { readBoundedResponse } from "../previewIO";
import { errorMessage } from "../../../shared/api";
import { ErrorNotice, Spinner } from "../../../shared/components";
import "./BookPreview.css";
import BookImage from "./BookImage";

type Entry = { name: string; label: string };
type Book = { kind: "epub" | "comic"; title?: string; entries: Entry[] };
type Node =
    | string
    | {
          tag: string;
          children: Node[];
          src?: string;
          alt?: string;
          href?: string;
          id?: string;
      };
const tags = new Set(
    "p div span h1 h2 h3 h4 h5 h6 strong b em i u s a img ul ol li blockquote pre code br hr table thead tbody tr th td caption dl dt dd sup sub figure figcaption section article".split(
        " ",
    ),
);
const positionKey = "tgdrive.reader.positions";
function positions(): Record<string, string> {
    try {
        const data = JSON.parse(localStorage.getItem(positionKey) || "{}");
        return data && typeof data === "object" && !Array.isArray(data)
            ? data
            : {};
    } catch {
        return {};
    }
}
function safeEntry(name: string) {
    return (
        name.length > 0 &&
        name.length <= 1024 &&
        !name.startsWith("/") &&
        !/^[a-z][a-z0-9+.-]*:/i.test(name) &&
        !name.includes("\\") &&
        !/[\x00-\x1f]/.test(name) &&
        !name.split("/").some((part) => part === ".." || part === ".")
    );
}
export function bookUrl(path: string, auth: string, entry?: string) {
    const params = new URLSearchParams({ path });
    if (auth) params.set("auth", auth);
    if (entry) params.set("entry", entry);
    return "/api/v1/books?" + params;
}
export async function bookJson(url: string, signal: AbortSignal) {
    const response = await fetch(url, {
        credentials: "same-origin",
        signal: AbortSignal.any([signal, AbortSignal.timeout(60000)]),
        headers: { Accept: "application/json" },
    });
    const data = JSON.parse(
        new TextDecoder().decode(
            await readBoundedResponse(response, 8 * 1024 * 1024, signal),
        ),
    );
    if (!response.ok)
        throw new Error(
            typeof data.detail === "string"
                ? data.detail
                : "The book could not be read. Try again.",
        );
    return data;
}
export function validateBook(data: Book): Book {
    if (
        !data ||
        !["epub", "comic"].includes(data.kind) ||
        !Array.isArray(data.entries) ||
        !data.entries.length ||
        data.entries.length > 10000 ||
        !data.entries.every(
            (entry) =>
                entry &&
                typeof entry.name === "string" &&
                safeEntry(entry.name) &&
                typeof entry.label === "string" &&
                entry.label.length <= 1024,
        )
    )
        throw new Error("The server returned an unreadable book index.");
    return data;
}
export function safeNodes(data: unknown): Node[] {
    let count = 0;
    function visit(value: unknown, depth: number): Node {
        if (++count > 20000 || depth > 100)
            throw new Error("This chapter is too complex to display.");
        if (typeof value === "string") return value;
        const node = value as Exclude<Node, string>;
        if (
            !node ||
            typeof node !== "object" ||
            !tags.has(node.tag) ||
            !Array.isArray(node.children)
        )
            throw new Error("The server returned an unreadable chapter.");
        return {
            tag: node.tag,
            children: node.children.map((child) => visit(child, depth + 1)),
            src:
                typeof node.src === "string" &&
                safeEntry(node.src.split("#")[0])
                    ? node.src.split("#")[0]
                    : undefined,
            href:
                typeof node.href === "string" &&
                safeEntry(node.href.split("#")[0])
                    ? node.href
                    : undefined,
            alt: typeof node.alt === "string" ? node.alt.slice(0, 500) : "",
            id: typeof node.id === "string" ? node.id.slice(0, 128) : undefined,
        };
    }
    if (!Array.isArray(data)) throw new Error("The chapter could not be read.");
    return data.map((node) => visit(node, 0));
}
export function bookEntryLabel(entry: Entry, index: number): string {
    if (!/\.(xhtml|html?)$/i.test(entry.label)) return entry.label;
    if (index === 0 || /cover/i.test(entry.label)) return "Cover";
    if (/wrap|spine/i.test(entry.label)) return "Illustration " + (index + 1);
    return "Section " + (index + 1);
}
export function isIllustratedPage(nodes: Node[]): boolean {
    let images = 0;
    let textLength = 0;
    const visit = (node: Node) => {
        if (typeof node === "string") textLength += node.trim().length;
        else {
            if (node.tag === "img" && node.src) images++;
            node.children.forEach(visit);
        }
    };
    nodes.forEach(visit);
    return images > 0 && textLength <= 80;
}
function readerPreferences() {
    try {
        const value = JSON.parse(
            localStorage.getItem("tgdrive.reader.preferences") || "{}",
        );
        return {
            font: [16, 18, 20, 22, 24].includes(value.font)
                ? Number(value.font)
                : 18,
            typeface: value.typeface === "sans" ? "sans" : "serif",
        };
    } catch {
        return { font: 18, typeface: "serif" };
    }
}
export default function BookPreview({
    path,
    auth,
    toolbarTarget,
}: {
    path: string;
    auth: string;
    toolbarTarget: HTMLElement | null;
}) {
    const [book, setBook] = useState<Book | null>(null);
    const [selected, setSelected] = useState("");
    const [nodes, setNodes] = useState<Node[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [indexAttempt, setIndexAttempt] = useState(0);
    const [attempt, setAttempt] = useState(0);
    const [rtl, setRtl] = useState(false);
    const [font, setFont] = useState(() => readerPreferences().font);
    const [typeface, setTypeface] = useState(
        () => readerPreferences().typeface,
    );
    useEffect(() => {
        try {
            localStorage.setItem(
                "tgdrive.reader.preferences",
                JSON.stringify({ font, typeface }),
            );
        } catch {
            /* Reading works without persistence. */
        }
    }, [font, typeface]);
    const [fit, setFit] = useState<"width" | "page">("width");
    const content = useRef<HTMLDivElement>(null);
    const fragment = useRef("");
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        setError("");
        void bookJson(bookUrl(path, auth), controller.signal)
            .then(validateBook)
            .then((result) => {
                if (controller.signal.aborted) return;
                setBook(result);
                const saved = positions()[path];
                setSelected(
                    result.entries.some((entry) => entry.name === saved)
                        ? saved
                        : result.entries[0].name,
                );
                if (result.kind === "comic") setLoading(false);
            })
            .catch((cause) => {
                if (!controller.signal.aborted) {
                    setError(errorMessage(cause));
                    setLoading(false);
                }
            });
        return () => controller.abort();
    }, [path, auth, indexAttempt]);
    useEffect(() => {
        if (!book || !selected) return;
        try {
            const saved = positions();
            delete saved[path];
            saved[path] = selected;
            localStorage.setItem(
                positionKey,
                JSON.stringify(
                    Object.fromEntries(Object.entries(saved).slice(-50)),
                ),
            );
        } catch {
            /* Reader works with storage disabled. */
        }
        content.current?.scrollTo({ top: 0 });
        if (book.kind !== "epub") return;
        const controller = new AbortController();
        setLoading(true);
        setError("");
        setNodes([]);
        void bookJson(bookUrl(path, auth, selected), controller.signal)
            .then((result) => {
                if (result.kind !== "chapter")
                    throw new Error(
                        "This EPUB chapter could not be displayed.",
                    );
                const chapter = safeNodes(result.nodes);
                if (!controller.signal.aborted) {
                    setNodes(chapter);
                    setLoading(false);
                }
            })
            .catch((cause) => {
                if (!controller.signal.aborted) {
                    setError(errorMessage(cause));
                    setLoading(false);
                }
            });
        return () => controller.abort();
    }, [path, auth, book, selected, attempt]);
    useEffect(() => {
        if (!loading && fragment.current) {
            document
                .getElementById("book-anchor-" + fragment.current)
                ?.scrollIntoView({ block: "start" });
            fragment.current = "";
        }
    }, [loading, nodes]);
    const index =
        book?.entries.findIndex((entry) => entry.name === selected) ?? 0;
    const move = (delta: number) => {
        if (!book) return;
        const entry = book.entries[index + delta];
        if (entry) {
            setError("");
            setSelected(entry.name);
            setAttempt(0);
        }
    };
    const follow = (href?: string) => {
        if (!href || !book) return;
        const [entry, id] = href.split("#");
        if (entry === selected && id) {
            document
                .getElementById("book-anchor-" + id)
                ?.scrollIntoView({ block: "start" });
            return;
        }
        if (book.entries.some((item) => item.name === entry)) {
            fragment.current = id || "";
            setSelected(entry);
        }
    };
    const render = (node: Node, key: number): ReactNode => {
        if (typeof node === "string") return node;
        const id = node.id ? "book-anchor-" + node.id : undefined;
        const children = node.children.map(render);
        if (node.tag === "img")
            return node.src ? (
                <BookImage
                    key={key}
                    id={id}
                    src={bookUrl(path, auth, node.src)}
                    alt={node.alt || ""}
                />
            ) : null;
        if (node.tag === "a")
            return node.href ? (
                <button
                    key={key}
                    id={id}
                    className="book-reference"
                    onClick={() => follow(node.href)}
                >
                    {children}
                </button>
            ) : (
                <span key={key} id={id}>
                    {children}
                </span>
            );
        return createElement(
            node.tag,
            { key, id },
            ...(node.tag === "br" || node.tag === "hr" ? [] : children),
        );
    };
    const toolbar = book && (
        <div className="book-toolbar" role="group" aria-label="Reader controls">
            <button
                className="icon-button"
                aria-label="Previous chapter or page"
                disabled={index <= 0}
                onClick={() => move(-1)}
            >
                {rtl ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
            </button>
            <label className="sr-only" htmlFor="book-entry">
                {book.kind === "epub" ? "Chapter" : "Page"}
            </label>
            <select
                id="book-entry"
                value={selected}
                onChange={(event) => setSelected(event.target.value)}
            >
                {book.entries.map((entry, i) => (
                    <option key={i} value={entry.name}>
                        {bookEntryLabel(entry, i)}
                    </option>
                ))}
            </select>
            <span className="book-position" aria-live="polite">
                {index + 1} / {book.entries.length}
            </span>
            <button
                className="icon-button"
                aria-label="Next chapter or page"
                disabled={index >= book.entries.length - 1}
                onClick={() => move(1)}
            >
                {rtl ? <ChevronLeft size={20} /> : <ChevronRight size={20} />}
            </button>
            <details
                className="book-options"
                onKeyDown={(event) => {
                    if (event.key === "Escape") {
                        event.preventDefault();
                        event.stopPropagation();
                        event.currentTarget.open = false;
                        event.currentTarget.querySelector("summary")?.focus();
                    }
                }}
            >
                <summary className="icon-button" aria-label="Reading options">
                    <Settings2 size={19} />
                </summary>
                <div className="book-options-panel">
                    {book.kind === "epub" ? (
                        <>
                            <button
                                className="icon-button"
                                aria-label="Decrease text size"
                                disabled={font <= 16}
                                onClick={() => setFont((n) => n - 2)}
                            >
                                <Minus size={17} />
                            </button>
                            <span>{font}px</span>
                            <button
                                className="icon-button"
                                aria-label="Increase text size"
                                disabled={font >= 24}
                                onClick={() => setFont((n) => n + 2)}
                            >
                                <Plus size={17} />
                            </button>
                            <label className="book-typeface-control">
                                Typeface
                                <select
                                    value={typeface}
                                    onChange={(event) =>
                                        setTypeface(event.target.value)
                                    }
                                >
                                    <option value="serif">Serif</option>
                                    <option value="sans">Sans serif</option>
                                </select>
                            </label>
                        </>
                    ) : null}
                    <>
                        <label className="sr-only" htmlFor="comic-fit">
                            Page fit
                        </label>
                        <select
                            id="comic-fit"
                            value={fit}
                            onChange={(event) =>
                                setFit(event.target.value as typeof fit)
                            }
                        >
                            <option value="width">Fit width</option>
                            <option value="page">Fit page</option>
                        </select>
                        <label className="book-direction">
                            <input
                                type="checkbox"
                                checked={rtl}
                                onChange={(event) =>
                                    setRtl(event.target.checked)
                                }
                            />
                            Right to left
                        </label>
                    </>
                </div>
            </details>
        </div>
    );
    return (
        <div
            className={
                "book-reader book-font-" + font + " book-typeface-" + typeface
            }
        >
            {toolbarTarget && toolbar
                ? createPortal(toolbar, toolbarTarget)
                : toolbar}
            {error ? (
                <div className="book-message">
                    <ErrorNotice message={error} />
                    <button
                        className="secondary-button"
                        onClick={() =>
                            book
                                ? (setError(""), setAttempt((n) => n + 1))
                                : setIndexAttempt((n) => n + 1)
                        }
                    >
                        <RefreshCw size={16} />
                        Try again
                    </button>
                </div>
            ) : null}
            {loading && (
                <div className="book-message" role="status">
                    <Spinner />
                    Loading {book?.kind === "epub" ? "chapter" : "book"}…
                </div>
            )}
            <div
                ref={content}
                className={"book-content book-fit-" + fit}
                tabIndex={0}
                aria-label={
                    book?.kind === "epub" ? "Book chapter" : "Comic page"
                }
                onKeyDown={(event) => {
                    if (
                        (event.target as HTMLElement).closest(
                            "button,input,select,a",
                        ) ||
                        event.ctrlKey ||
                        event.metaKey ||
                        event.altKey
                    )
                        return;
                    if (
                        event.key === "ArrowRight" ||
                        event.key === "ArrowLeft"
                    ) {
                        event.preventDefault();
                        move(
                            (event.key === "ArrowRight" ? 1 : -1) *
                                (rtl ? -1 : 1),
                        );
                    }
                }}
            >
                {!error && book?.kind === "comic" && selected && (
                    <BookImage
                        key={selected + attempt}
                        eager
                        src={bookUrl(path, auth, selected)}
                        alt={
                            "Page " + (index + 1) + " of " + book.entries.length
                        }
                    />
                )}
                {!error && !loading && book?.kind === "epub" && (
                    <article
                        className={
                            "book-chapter" +
                            (isIllustratedPage(nodes)
                                ? " book-illustrated-page"
                                : "")
                        }
                    >
                        {nodes.map(render)}
                    </article>
                )}
            </div>
        </div>
    );
}
