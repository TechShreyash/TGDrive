import {
    checkPdfSize,
    PdfRangeReader,
    PDF_RANGE_CHUNK_BYTES,
} from "./pdfRange";
import {
    useCallback,
    useEffect,
    useId,
    useLayoutEffect,
    useMemo,
    useRef,
    useState,
    type FormEvent,
} from "react";
import {
    Check,
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    Minus,
    Plus,
    ZoomIn,
} from "lucide-react";
import { createPortal } from "react-dom";
import {
    getDocument,
    PDFDataRangeTransport,
    GlobalWorkerOptions,
    TextLayer,
    PasswordResponses,
    version as pdfjsVersion,
    type PDFDocumentLoadingTask,
    type PDFDocumentProxy,
    type PDFPageProxy,
    type RenderTask,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.mjs?worker&url";
import { ErrorNotice, Spinner } from "../../../shared/components";
import { errorMessage } from "../../../shared/api";
import PageJump from "../PageJump";
import { useMediaQuery } from "@mantine/hooks";
import type { VideoProgressMode } from "../video/videoWatchProgress";
import {
    createPdfProgressWriter,
    loadServerPdfPosition,
    pdfResumePage,
    readLocalPdfPosition,
    saveLocalPdfPosition,
    type PdfPosition,
} from "./pdfProgress";
import "./PdfPreview.css";

GlobalWorkerOptions.workerSrc = workerUrl;
const OVERSCAN_PAGE_TARGET = 5;
const MAX_PDF_PAGES = 10_000;
const MAX_PAGE_PIXELS = 2_000_000;
const MAX_TOTAL_PIXELS = 10_000_000;
const MAX_CANVAS_SIDE = 4096;
const MAX_TEXT_RUNS = 2500;
const MAX_TEXT_CHARACTERS = 100_000;
const MAX_TOTAL_TEXT_RUNS = 12_500;
const MAX_TOTAL_TEXT_CHARACTERS = 500_000;
// Fit Width caps at a readable page width instead of stretching to any monitor.
const PDF_READING_WIDTH = 960;
const PAGE_GAP = 24;
const PDF_RESOURCE_BASE = `/static/pdfjs/${pdfjsVersion}/`;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 5;
const ZOOM_PRESETS = [0.5, 0.75, 1, 1.25, 1.5, 2];
// Below this width (or on touch) zoom and fit move into one options menu.
const COMPACT_PDF_QUERY = "(max-width: 1100px), (pointer: coarse)";
type PageSize = { width: number; height: number };
type PageFrame = PageSize & { top: number };
type Layout = { pages: PageFrame[]; width: number; height: number };

function pageSize(page: PDFPageProxy): PageSize {
    const { width, height } = page.getViewport({ scale: 1 });
    if (
        !Number.isFinite(width) ||
        !Number.isFinite(height) ||
        width <= 0 ||
        height <= 0
    )
        throw new Error(
            "This PDF page has invalid dimensions. Download the original instead.",
        );
    return { width, height };
}

/** Keep selectable text bounded too; file text is handed only to PDF.js textContent. */
function boundedText(
    source: ReadableStream,
    maxRuns: number,
    maxCharacters: number,
): ReadableStream {
    const reader = source.getReader();
    let runs = 0;
    let characters = 0;
    let stopped = false;
    return new ReadableStream({
        async pull(controller) {
            try {
                const { value, done } = await reader.read();
                if (done) {
                    stopped = true;
                    controller.close();
                    return;
                }
                const items = [];
                for (const item of value.items) {
                    if (typeof item.str !== "string") continue;
                    if (runs >= maxRuns || characters >= maxCharacters) break;
                    const str = item.str.slice(0, maxCharacters - characters);
                    items.push({ ...item, str });
                    runs += 1;
                    characters += str.length;
                }
                controller.enqueue({ ...value, items });
                if (runs >= maxRuns || characters >= maxCharacters) {
                    stopped = true;
                    controller.close();
                    await reader.cancel();
                }
            } catch (error) {
                if (!stopped) controller.error(error);
            }
        },
        async cancel(reason) {
            stopped = true;
            await reader.cancel(reason);
        },
    });
}

function pageAt(pages: PageFrame[], position: number): number {
    let low = 0;
    let high = pages.length - 1;
    while (low < high) {
        const middle = Math.floor((low + high + 1) / 2);
        if (pages[middle].top <= position) low = middle;
        else high = middle - 1;
    }
    return low;
}

function PdfPage({
    document,
    number,
    name,
    frame,
    pixelBudget,
    textRunBudget,
    textCharacterBudget,
    onMeasure,
    onReady,
    received,
}: {
    document: PDFDocumentProxy;
    number: number;
    name: string;
    frame: PageFrame;
    pixelBudget: number;
    textRunBudget: number;
    textCharacterBudget: number;
    onMeasure: (number: number, size: PageSize) => void;
    onReady: () => void;
    received: number;
}) {
    const canvas = useRef<HTMLCanvasElement>(null);
    const textContainer = useRef<HTMLDivElement>(null);
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState("");
    const [textUnavailable, setTextUnavailable] = useState(false);
    useEffect(() => {
        let cancelled = false;
        let page: PDFPageProxy | undefined;
        let render: RenderTask | undefined;
        let textLayer: TextLayer | undefined;
        const target = canvas.current;
        const text = textContainer.current;
        setBusy(true);
        setError("");
        setTextUnavailable(false);
        void (async () => {
            page = await document.getPage(number);
            if (cancelled || !target || !text) {
                page.cleanup();
                return;
            }
            const base = pageSize(page);
            onMeasure(number, base);
            const viewport = page.getViewport({
                scale: frame.width / base.width,
            });
            // Visible pages share ten million backing pixels (40 MB RGBA), with a
            // two-million-pixel per-page ceiling. More pages may fit at low zoom.
            // Large zoom levels retain CSS size while reducing backing resolution.
            const outputScale = Math.min(
                window.devicePixelRatio || 1,
                2,
                Math.sqrt(pixelBudget / (viewport.width * viewport.height)),
                MAX_CANVAS_SIDE / viewport.width,
                MAX_CANVAS_SIDE / viewport.height,
            );
            target.width = Math.min(
                pixelBudget,
                Math.max(1, Math.floor(viewport.width * outputScale)),
            );
            target.height = Math.min(
                Math.floor(pixelBudget / target.width),
                Math.max(1, Math.floor(viewport.height * outputScale)),
            );
            render = page.render({
                canvas: target,
                viewport,
                transform: [
                    target.width / viewport.width,
                    0,
                    0,
                    target.height / viewport.height,
                    0,
                    0,
                ],
            });
            await render.promise;
            if (cancelled) return;
            setBusy(false);
            onReady();
            text.style.setProperty(
                "--total-scale-factor",
                String(viewport.scale * viewport.userUnit),
            );
            textLayer = new TextLayer({
                container: text,
                viewport,
                textContentSource: boundedText(
                    page.streamTextContent(),
                    textRunBudget,
                    textCharacterBudget,
                ),
            });
            try {
                await textLayer.render();
            } catch {
                if (!cancelled) setTextUnavailable(true);
            }
        })().catch((error) => {
            if (!cancelled) {
                setError(errorMessage(error));
                setBusy(false);
            }
        });
        return () => {
            cancelled = true;
            render?.cancel();
            textLayer?.cancel();
            text?.replaceChildren();
            if (target) {
                target.width = 0;
                target.height = 0;
            }
            // PDF.js defers cleanup if another render of this page is still active.
            void (render?.promise ?? Promise.resolve())
                .catch(() => {})
                .then(() => page?.cleanup());
        };
    }, [
        document,
        number,
        frame.width,
        pixelBudget,
        textRunBudget,
        textCharacterBudget,
        onMeasure,
        onReady,
    ]);
    return (
        <div
            className="pdf-page"
            data-page-number={number}
            role="group"
            aria-label={"Page " + number}
            aria-busy={busy}
            style={{
                top: frame.top,
                width: frame.width,
                height: frame.height,
            }}
        >
            {busy && (
                <div className="pdf-page-loading" role="status">
                    <Spinner /> Loading page {number}…
                    {received > 0 && (
                        <span>
                            {(received / 1024 / 1024).toFixed(1)} MiB received
                            for this PDF
                        </span>
                    )}
                </div>
            )}
            {error && (
                <div className="pdf-page-error">
                    <ErrorNotice message={error} />
                    <p>
                        Download the original to open this page on your device.
                    </p>
                </div>
            )}
            {textUnavailable && (
                <span className="sr-only">
                    Text selection is unavailable on this page. Download the
                    original for other reading options.
                </span>
            )}
            <canvas
                ref={canvas}
                role="img"
                aria-label={name + ", page " + number}
                hidden={busy || !!error}
            />
            <div
                ref={textContainer}
                className="pdf-text-layer"
                role="group"
                aria-label={"Selectable text, page " + number}
                hidden={busy || !!error}
            />
        </div>
    );
}

export default function PdfPreview({
    fileId,
    progressMode,
    url,
    name,
    size,
    toolbarTarget,
}: {
    fileId: string;
    progressMode: VideoProgressMode | null;
    url: string;
    name: string;
    size: number;
    toolbarTarget: HTMLElement | null;
}) {
    const [toolsOpen, setToolsOpen] = useState(false);
    const compact = useMediaQuery(COMPACT_PDF_QUERY, false, {
        getInitialValueInEffect: false,
    });
    const tools = useRef<HTMLDivElement>(null);
    const toolsToggle = useRef<HTMLButtonElement>(null);
    const toolsId = useId();
    useEffect(() => {
        if (!toolsOpen) return;
        const dismiss = (event: PointerEvent) => {
            if (
                event.target instanceof Node &&
                !tools.current?.contains(event.target)
            )
                setToolsOpen(false);
        };
        window.document.addEventListener("pointerdown", dismiss);
        return () =>
            window.document.removeEventListener("pointerdown", dismiss);
    }, [toolsOpen]);
    const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
    const documentRef = useRef(document);
    documentRef.current = document;
    const [sizes, setSizes] = useState<Map<number, PageSize>>(new Map());
    const [busy, setBusy] = useState(true);
    const [received, setReceived] = useState(0);
    const [firstPaint, setFirstPaint] = useState(false);
    const onReady = useCallback(() => setFirstPaint(true), []);
    const [error, setError] = useState("");
    const [savedPage, setSavedPage] = useState<PdfPosition | null>(null);
    const [progressReady, setProgressReady] = useState(false);
    const [progressNotice, setProgressNotice] = useState("");
    const progressRead = useRef<AbortController | null>(null);
    const progressWriter = useRef<ReturnType<
        typeof createPdfProgressWriter
    > | null>(null);
    const previousProgressMode = useRef(progressMode);
    const navigated = useRef(false);
    const restored = useRef(false);
    const lastSaved = useRef("");
    const currentPage = useRef(1);
    const closeProgress = useRef<(() => void) | null>(null);
    // Capture scroll before ref detachment, including a pending animation frame.
    useLayoutEffect(() => () => closeProgress.current?.(), [fileId, url, size]);
    useEffect(() => {
        const controller = new AbortController();
        progressRead.current = controller;
        const previous = previousProgressMode.current;
        previousProgressMode.current = progressMode;
        if (
            progressMode === "server" &&
            (previous === "share" || previous === "demo")
        )
            navigated.current = false;
        restored.current = false;
        lastSaved.current = "";
        setSavedPage(null);
        setProgressReady(false);
        setProgressNotice("");
        const writer =
            progressMode === "server"
                ? createPdfProgressWriter(fileId, setProgressNotice)
                : null;
        progressWriter.current = writer;
        if (progressMode === "server" && navigated.current) {
            setProgressReady(true);
        } else if (progressMode === "server") {
            void loadServerPdfPosition(fileId, controller.signal)
                .then((saved) => {
                    if (controller.signal.aborted) return;
                    setSavedPage(saved);
                    setProgressReady(true);
                })
                .catch(() => {
                    if (controller.signal.aborted) return;
                    setProgressReady(true);
                    setProgressNotice(
                        "Saved PDF page is unavailable. Reading still works; navigate to choose a new saved page.",
                    );
                });
        } else if (progressMode === "share" || progressMode === "demo") {
            try {
                setSavedPage(readLocalPdfPosition(fileId, progressMode));
            } catch {
                setProgressNotice(
                    "Browser storage is unavailable. Your PDF page cannot be saved on this device.",
                );
            }
            setProgressReady(true);
        } else if (progressMode === "unavailable") {
            setProgressReady(true);
            setProgressNotice(
                "PDF resume access could not be checked. Reading still works; reopen the PDF to check again.",
            );
        }
        const close = () => {
            const currentWriter = progressWriter.current;
            const count = documentRef.current?.numPages;
            const pages = layoutRef.current.pages;
            if (
                navigated.current &&
                restored.current &&
                count &&
                pages.length === count
            ) {
                const page = scroll.current
                    ? pageAt(pages, scroll.current.scrollTop + 24) + 1
                    : currentPage.current;
                currentPage.current = page;
                if (currentWriter) {
                    currentWriter.start();
                    currentWriter.save(page, count);
                } else if (
                    progressMode === "share" ||
                    progressMode === "demo"
                ) {
                    try {
                        saveLocalPdfPosition(fileId, progressMode, page, count);
                    } catch {
                        // Closing must still release PDF.js when storage is blocked.
                    }
                }
            }
            currentWriter?.close();
        };
        closeProgress.current = close;
        window.addEventListener("pagehide", close);
        return () => {
            controller.abort();
            close();
            window.removeEventListener("pagehide", close);
        };
    }, [fileId, progressMode, url, size]);
    const choosePosition = () => {
        navigated.current = true;
        progressRead.current?.abort();
        setProgressReady(true);
    };
    const [zoom, setZoom] = useState<number | null>(null);
    const [passwordChallenge, setPasswordChallenge] = useState<{
        reason: "required" | "incorrect";
    } | null>(null);
    const passwordReason = passwordChallenge?.reason ?? null;
    const [password, setPassword] = useState("");
    const passwordInput = useRef<HTMLInputElement>(null);
    const openingError = useRef<HTMLDivElement>(null);
    const providePassword = useRef<((value: string) => void) | null>(null);
    const cancelOpening = useRef<(() => void) | null>(null);
    useEffect(() => {
        if (passwordChallenge) passwordInput.current?.focus();
    }, [passwordChallenge]);
    useEffect(() => {
        if (error) openingError.current?.focus();
    }, [error]);

    function unlock(event: FormEvent) {
        event.preventDefault();
        const provide = providePassword.current;
        if (!provide || !password) return;
        providePassword.current = null;
        setPasswordChallenge(null);
        setPassword("");
        provide(password);
    }
    const [fit, setFit] = useState<"width" | "page">("width");
    const [view, setView] = useState({ top: 0, width: 800, height: 700 });
    const viewRef = useRef(view);
    viewRef.current = view;
    const scroll = useRef<HTMLDivElement>(null);
    const layoutRef = useRef<Layout>({ pages: [], width: 0, height: 0 });
    const anchor = useRef<{ index: number; fraction: number } | null>(null);
    const animation = useRef(0);
    const syncView = useCallback(() => {
        const target = scroll.current;
        if (!target) return;
        setView((old) => {
            const next = {
                top: target.scrollTop,
                width: target.clientWidth,
                height: target.clientHeight,
            };
            return old.top === next.top &&
                old.width === next.width &&
                old.height === next.height
                ? old
                : next;
        });
    }, []);
    const rememberAnchor = useCallback(() => {
        const target = scroll.current;
        const pages = layoutRef.current.pages;
        if (!target || !pages.length || anchor.current) return;
        const index = pageAt(pages, target.scrollTop);
        anchor.current = {
            index,
            fraction:
                (target.scrollTop - pages[index].top) / pages[index].height,
        };
    }, []);
    const measure = useCallback(
        (number: number, size: PageSize) => {
            setSizes((old) => {
                const previous = old.get(number);
                if (
                    previous?.width === size.width &&
                    previous?.height === size.height
                )
                    return old;
                rememberAnchor();
                return new Map(old).set(number, size);
            });
        },
        [rememberAnchor],
    );

    useEffect(() => {
        const controller = new AbortController();
        let task: PDFDocumentLoadingTask | undefined;
        let reader: PdfRangeReader | undefined;
        const fail = (error: unknown) => {
            if (controller.signal.aborted) return;
            setError(errorMessage(error));
            setBusy(false);
            setDocument(null);
            setPasswordChallenge(null);
            setPassword("");
            providePassword.current = null;
            controller.abort();
            reader?.close();
            void task
                ?.destroy()
                .then(() => TextLayer.cleanup())
                .catch(() => {});
        };
        setBusy(true);
        navigated.current = false;
        restored.current = false;
        setFirstPaint(false);
        setReceived(0);
        setError("");
        setPasswordChallenge(null);
        setPassword("");
        providePassword.current = null;
        cancelOpening.current = () => fail(new Error("PDF opening cancelled."));
        setDocument(null);
        setSizes(new Map());
        setZoom(null);
        setFit("width");
        anchor.current = null;
        if (scroll.current) scroll.current.scrollTop = 0;
        void (async () => {
            checkPdfSize(size);
            reader = new PdfRangeReader(url, size, (value) => {
                if (!controller.signal.aborted) setReceived(value);
            });
            const ranges = reader;
            const transport = new (class extends PDFDataRangeTransport {
                requestDataRange(begin: number, end: number) {
                    void ranges
                        .read(begin, end)
                        .then((bytes) => {
                            if (!controller.signal.aborted)
                                this.onDataRange(begin, bytes);
                        })
                        .catch(fail);
                }
                abort() {
                    ranges.close();
                }
            })(size, null);
            task = getDocument({
                range: transport,
                rangeChunkSize: PDF_RANGE_CHUNK_BYTES,
                disableStream: true,
                disableAutoFetch: true,
                enableXfa: false,
                useWorkerFetch: true,
                useSystemFonts: false,
                cMapUrl: PDF_RESOURCE_BASE + "cmaps/",
                cMapPacked: true,
                standardFontDataUrl: PDF_RESOURCE_BASE + "standard_fonts/",
                wasmUrl: PDF_RESOURCE_BASE + "wasm/",
                iccUrl: PDF_RESOURCE_BASE + "iccs/",
                maxImageSize: 16777216,
                canvasMaxAreaInBytes: 16777216,
            });
            task.onPassword = (
                provide: (value: string) => void,
                reason: number,
            ) => {
                if (controller.signal.aborted) return;
                providePassword.current = provide;
                setPassword("");
                setPasswordChallenge({
                    reason:
                        reason === PasswordResponses.INCORRECT_PASSWORD
                            ? "incorrect"
                            : "required",
                });
            };
            const loaded = await task.promise;
            if (loaded.numPages > MAX_PDF_PAGES)
                throw new Error(
                    "PDF preview is limited to 10,000 pages. Download the original instead.",
                );
            const first = await loaded.getPage(1);
            if (controller.signal.aborted) return;
            setSizes(new Map([[1, pageSize(first)]]));
            setDocument(loaded);
            setBusy(false);
        })().catch(fail);
        return () => {
            controller.abort();
            providePassword.current = null;
            cancelOpening.current = null;
            reader?.close();
            void task
                ?.destroy()
                .then(() => TextLayer.cleanup())
                .catch(() => {});
        };
    }, [url, size]);

    const firstSize = sizes.get(1) ?? { width: 612, height: 792 };
    const fitWidth = Math.max(64, Math.min(view.width - 32, PDF_READING_WIDTH));
    const fitHeight = Math.max(64, view.height - 32);
    const layout = useMemo<Layout>(() => {
        if (!document) return { pages: [], width: view.width, height: 0 };
        let top = 16;
        let width = view.width;
        const pages = Array.from({ length: document.numPages }, (_, index) => {
            const base = sizes.get(index + 1) ?? firstSize;
            const pageScale =
                zoom ??
                (fit === "page"
                    ? Math.min(fitWidth / base.width, fitHeight / base.height)
                    : fitWidth / base.width);
            const frame = {
                top,
                width: base.width * pageScale,
                height: Math.max(1, base.height * pageScale),
            };
            top += frame.height + PAGE_GAP;
            width = Math.max(width, frame.width + 32);
            return frame;
        });
        return { pages, width, height: Math.max(0, top - PAGE_GAP + 16) };
    }, [
        document,
        sizes,
        firstSize,
        fitWidth,
        fitHeight,
        fit,
        zoom,
        view.width,
    ]);
    layoutRef.current = layout;
    useLayoutEffect(() => {
        const target = scroll.current;
        const remembered = anchor.current;
        if (target && remembered && layout.pages[remembered.index]) {
            const page = layout.pages[remembered.index];
            target.scrollTop = Math.max(
                0,
                page.top + remembered.fraction * page.height,
            );
            anchor.current = null;
        }
        syncView();
    }, [layout, syncView]);

    useEffect(() => {
        const target = scroll.current;
        if (!target) return;
        const observer = new ResizeObserver(() => {
            if (target.clientWidth !== viewRef.current.width) rememberAnchor();
            syncView();
        });
        observer.observe(target);
        syncView();
        return () => {
            observer.disconnect();
            cancelAnimationFrame(animation.current);
        };
    }, [rememberAnchor, syncView]);

    const current = layout.pages.length
        ? pageAt(layout.pages, view.top + 24)
        : 0;
    currentPage.current = current + 1;
    useLayoutEffect(() => {
        if (
            !document ||
            !progressReady ||
            restored.current ||
            !layout.pages.length ||
            view.width <= 0 ||
            view.height <= 0 ||
            view.width !== scroll.current?.clientWidth ||
            view.height !== scroll.current?.clientHeight
        )
            return;
        restored.current = true;
        if (navigated.current || !savedPage) return;
        const page = pdfResumePage(savedPage, document.numPages);
        if (page === null) {
            setProgressNotice(
                "The saved page belongs to a different PDF page count. Reading starts at page 1.",
            );
            return;
        }
        scroll.current?.scrollTo({ top: layout.pages[page - 1].top });
        syncView();
    }, [
        document,
        progressReady,
        savedPage,
        layout,
        syncView,
        view.width,
        view.height,
    ]);
    useEffect(() => {
        if (
            !document ||
            !firstPaint ||
            !progressReady ||
            !restored.current ||
            !navigated.current
        )
            return;
        const page = current + 1;
        const key = page + ":" + document.numPages;
        if (lastSaved.current === key) return;
        lastSaved.current = key;
        if (progressMode === "server") {
            if (progressWriter.current?.closed)
                progressWriter.current = createPdfProgressWriter(
                    fileId,
                    setProgressNotice,
                );
            progressWriter.current?.start();
            progressWriter.current?.save(page, document.numPages);
        } else if (progressMode === "share" || progressMode === "demo") {
            // Coalesce scrolling; close/pagehide still flush the actual final page.
            const timer = setTimeout(() => {
                try {
                    saveLocalPdfPosition(
                        fileId,
                        progressMode,
                        page,
                        document.numPages,
                    );
                    setProgressNotice("");
                } catch {
                    setProgressNotice(
                        "Browser storage is unavailable. Your PDF page cannot be saved on this device.",
                    );
                }
            }, 500);
            return () => clearTimeout(timer);
        }
    }, [current, document, firstPaint, progressReady, progressMode, fileId]);
    const currentSize = sizes.get(current + 1) ?? firstSize;
    const scale =
        zoom ??
        (fit === "page"
            ? Math.min(
                  fitWidth / currentSize.width,
                  fitHeight / currentSize.height,
              )
            : fitWidth / currentSize.width);
    function fitView(mode: "width" | "page") {
        rememberAnchor();
        if (mode === "page") anchor.current = { index: current, fraction: 0 };
        if (scroll.current) scroll.current.scrollLeft = 0;
        setFit(mode);
        setZoom(null);
        closeMenu();
    }
    function closeMenu() {
        // The phone options sheet stays open for repeated changes; the desktop menu closes.
        if (compact) return;
        setToolsOpen(false);
        toolsToggle.current?.focus();
    }
    let firstVisible = layout.pages.length ? pageAt(layout.pages, view.top) : 0;
    if (
        firstVisible < layout.pages.length - 1 &&
        layout.pages[firstVisible].top + layout.pages[firstVisible].height <=
            view.top
    )
        firstVisible += 1;
    let lastVisible = layout.pages.length
        ? pageAt(layout.pages, view.top + view.height)
        : 0;
    if (
        lastVisible > firstVisible &&
        layout.pages[lastVisible].top >= view.top + view.height
    )
        lastVisible -= 1;
    const visibleCount = Math.max(1, lastVisible - firstVisible + 1);
    // Five is an overscan target, never a cap on pages that intersect the view.
    const targetCount = Math.max(
        firstPaint ? OVERSCAN_PAGE_TARGET : 1,
        visibleCount,
    );
    // Readers mostly move forward: keep one page behind, spend the rest ahead.
    const start = Math.max(
        0,
        Math.min(
            firstVisible - Math.min(1, targetCount - visibleCount),
            layout.pages.length - targetCount,
        ),
    );
    const activePages = layout.pages.slice(start, start + targetCount);
    const activeCount = Math.max(1, activePages.length);
    const pixelBudget = Math.min(
        MAX_PAGE_PIXELS,
        Math.floor(MAX_TOTAL_PIXELS / activeCount),
    );
    const textRunBudget = Math.min(
        MAX_TEXT_RUNS,
        Math.floor(MAX_TOTAL_TEXT_RUNS / activeCount),
    );
    const textCharacterBudget = Math.min(
        MAX_TEXT_CHARACTERS,
        Math.floor(MAX_TOTAL_TEXT_CHARACTERS / activeCount),
    );
    function setScale(next: number | null) {
        const normalized =
            next === null ? null : Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next));
        if (normalized === zoom) return;
        rememberAnchor();
        setZoom(normalized);
    }
    function goToPage(page: number) {
        choosePosition();
        scroll.current?.scrollTo({ top: layout.pages[page - 1]?.top ?? 0 });
        syncView();
    }
    const zoomLabel = (
        <span className="pdf-zoom" id={toolsId + "-zoom"} aria-label="PDF zoom">
            {document ? Math.round(scale * 100) + "%" : "—"}
        </span>
    );
    return (
        <div className="pdf-preview">
            {toolbarTarget &&
                createPortal(
                    <div
                        ref={tools}
                        className="pdf-controls"
                        data-open={toolsOpen}
                        onKeyDown={(event) => {
                            if (event.key === "Escape" && toolsOpen) {
                                event.preventDefault();
                                event.stopPropagation();
                                setToolsOpen(false);
                                toolsToggle.current?.focus();
                            }
                        }}
                        onBlur={(event) => {
                            if (
                                !event.currentTarget.contains(
                                    event.relatedTarget,
                                )
                            )
                                setToolsOpen(false);
                        }}
                    >
                        <div
                            className="reader-navigation"
                            role="group"
                            aria-label="Page navigation"
                        >
                            <button
                                className="icon-button"
                                aria-label="Previous page"
                                title="Previous page"
                                disabled={!document || current === 0}
                                onClick={() => goToPage(current)}
                            >
                                <ChevronLeft size={20} />
                            </button>
                            <label
                                className="sr-only"
                                htmlFor={toolsId + "-page"}
                            >
                                Page
                            </label>
                            <PageJump
                                id={toolsId + "-page"}
                                current={current + 1}
                                total={document?.numPages ?? 0}
                                onJump={goToPage}
                            />
                            <span className="book-position" aria-live="polite">
                                {document && (
                                    <>
                                        <span className="sr-only">
                                            {current + 1}
                                        </span>{" "}
                                        / {document.numPages}
                                    </>
                                )}
                            </span>
                            <button
                                className="icon-button"
                                aria-label="Next page"
                                title="Next page"
                                disabled={
                                    !document ||
                                    current >= document.numPages - 1
                                }
                                onClick={() => goToPage(current + 2)}
                            >
                                <ChevronRight size={20} />
                            </button>
                        </div>
                        {progressNotice && (
                            <span className="pdf-progress-notice" role="status">
                                {progressNotice}
                            </span>
                        )}
                        {compact && (
                            <button
                                ref={toolsToggle}
                                type="button"
                                className="icon-button pdf-options-toggle"
                                aria-label="PDF View Options"
                                title="Zoom and fit"
                                aria-expanded={toolsOpen}
                                aria-controls={toolsId}
                                onClick={() => setToolsOpen((open) => !open)}
                            >
                                <ZoomIn size={20} />
                            </button>
                        )}
                        <div
                            className="pdf-toolbar"
                            id={toolsId}
                            role="group"
                            aria-label="PDF View Controls"
                            hidden={compact && !toolsOpen}
                        >
                            <div className="inline-actions">
                                <button
                                    className="icon-button"
                                    aria-label="Zoom Out"
                                    title="Zoom out"
                                    disabled={!document || scale <= MIN_ZOOM}
                                    onClick={() => setScale(scale - 0.25)}
                                >
                                    <Minus size={18} />
                                </button>
                                {compact ? (
                                    zoomLabel
                                ) : (
                                    <button
                                        ref={toolsToggle}
                                        type="button"
                                        className="pdf-zoom-toggle"
                                        aria-label="PDF View Options"
                                        aria-describedby={toolsId + "-zoom"}
                                        title="Zoom and fit"
                                        aria-expanded={toolsOpen}
                                        aria-controls={toolsId + "-menu"}
                                        onClick={() =>
                                            setToolsOpen((open) => !open)
                                        }
                                    >
                                        {zoomLabel}
                                        <ChevronDown size={14} />
                                    </button>
                                )}
                                <button
                                    className="icon-button"
                                    aria-label="Zoom in"
                                    title="Zoom in"
                                    disabled={!document || scale >= MAX_ZOOM}
                                    onClick={() => setScale(scale + 0.25)}
                                >
                                    <Plus size={18} />
                                </button>
                            </div>
                            <div
                                className="view-menu"
                                id={toolsId + "-menu"}
                                role="group"
                                aria-label="Zoom presets"
                                hidden={!compact && !toolsOpen}
                            >
                                {(
                                    [
                                        ["width", "Fit Width", "Fit width"],
                                        ["page", "Fit Page", "Fit page"],
                                    ] as const
                                ).map(([mode, label, text]) => (
                                    <button
                                        key={mode}
                                        type="button"
                                        className="view-menu-item pdf-fit"
                                        aria-label={label}
                                        aria-pressed={
                                            zoom === null && fit === mode
                                        }
                                        disabled={!document}
                                        onClick={() => fitView(mode)}
                                    >
                                        <Check size={16} aria-hidden="true" />
                                        {text}
                                    </button>
                                ))}
                                <hr />
                                {ZOOM_PRESETS.map((value) => (
                                    <button
                                        key={value}
                                        type="button"
                                        className="view-menu-item"
                                        aria-pressed={zoom === value}
                                        disabled={!document}
                                        onClick={() => {
                                            setScale(value);
                                            closeMenu();
                                        }}
                                    >
                                        <Check size={16} aria-hidden="true" />
                                        {value * 100}%
                                    </button>
                                ))}
                            </div>
                        </div>
                    </div>,
                    toolbarTarget,
                )}
            {passwordReason && (
                <form
                    className="pdf-password"
                    onSubmit={unlock}
                    aria-labelledby={toolsId + "-password-title"}
                >
                    <h2 id={toolsId + "-password-title"}>
                        Password-Protected PDF
                    </h2>
                    <p>Enter the document password to open it.</p>
                    <label htmlFor={toolsId + "-password"}>PDF Password</label>
                    <input
                        ref={passwordInput}
                        id={toolsId + "-password"}
                        type="password"
                        value={password}
                        autoComplete="off"
                        required
                        onChange={(event) => setPassword(event.target.value)}
                        aria-describedby={
                            passwordReason === "incorrect"
                                ? toolsId + "-password-error"
                                : undefined
                        }
                        aria-invalid={passwordReason === "incorrect"}
                    />
                    {passwordReason === "incorrect" && (
                        <p
                            className="error-notice"
                            role="alert"
                            id={toolsId + "-password-error"}
                        >
                            Incorrect password. Try again.
                        </p>
                    )}
                    <div className="inline-actions">
                        <button
                            type="button"
                            className="secondary-button"
                            onClick={() => cancelOpening.current?.()}
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            className="primary-button"
                            disabled={!password}
                        >
                            Open PDF
                        </button>
                    </div>
                </form>
            )}
            {busy && !passwordReason && (
                <div className="preview-loading" role="status">
                    <Spinner /> Loading PDF…
                    {received > 0 && (
                        <span>
                            {(received / 1024 / 1024).toFixed(1)} MiB received
                        </span>
                    )}
                </div>
            )}
            {error && (
                <div
                    className="preview-pdf-error"
                    ref={openingError}
                    tabIndex={-1}
                >
                    <ErrorNotice message={error} />
                    <p>
                        Download the original to open this PDF on your device.
                    </p>
                </div>
            )}
            <div
                className="pdf-scroll"
                ref={scroll}
                tabIndex={0}
                role="region"
                aria-label={name + " PDF pages"}
                hidden={busy || !!error}
                onPointerDown={choosePosition}
                onWheel={choosePosition}
                onTouchStart={choosePosition}
                onKeyDown={(event) => {
                    if (
                        [
                            "ArrowUp",
                            "ArrowDown",
                            "PageUp",
                            "PageDown",
                            "Home",
                            "End",
                            " ",
                        ].includes(event.key)
                    )
                        choosePosition();
                }}
                onScroll={() => {
                    cancelAnimationFrame(animation.current);
                    animation.current = requestAnimationFrame(syncView);
                }}
            >
                <div
                    className="pdf-pages"
                    style={{ width: layout.width, height: layout.height }}
                >
                    {activePages.map((frame, index) => (
                        <PdfPage
                            key={start + index + 1}
                            document={document!}
                            number={start + index + 1}
                            name={name}
                            frame={frame}
                            pixelBudget={pixelBudget}
                            textRunBudget={textRunBudget}
                            textCharacterBudget={textCharacterBudget}
                            onMeasure={measure}
                            onReady={onReady}
                            received={received}
                        />
                    ))}
                </div>
            </div>
        </div>
    );
}
