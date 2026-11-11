import { request } from "../../../shared/api";
import { createOrderedProgressWriter } from "../orderedProgressWriter";

export interface PdfPosition {
    page_number: number;
    page_count: number;
    updated_at: number;
}
type LocalScope = "share" | "demo";
const LOCAL_LIMIT = 200;

function position(value: unknown): PdfPosition | null {
    if (value === null) return null;
    const row = value as PdfPosition | undefined;
    if (
        !row ||
        !Number.isInteger(row.page_number) ||
        !Number.isInteger(row.page_count) ||
        row.page_number < 1 ||
        row.page_count < row.page_number ||
        row.page_count > 10_000 ||
        !Number.isFinite(row.updated_at) ||
        row.updated_at < 0
    )
        throw new Error("The saved PDF page could not be verified.");
    return {
        page_number: row.page_number,
        page_count: row.page_count,
        updated_at: row.updated_at,
    };
}

function endpoint(fileId: string) {
    return "/api/v1/files/" + encodeURIComponent(fileId) + "/pdf-progress";
}

export async function loadServerPdfPosition(
    fileId: string,
    signal: AbortSignal,
) {
    const result = await request<{ progress: unknown }>(
        endpoint(fileId),
        undefined,
        { signal, cache: "no-store", notifySessionExpiry: false },
    );
    return position(result.progress);
}

export function createPdfProgressWriter(
    fileId: string,
    onProblem: (message: string) => void,
) {
    const writer = createOrderedProgressWriter(
        endpoint(fileId),
        position,
        "pdf",
        onProblem,
    );
    return {
        get closed() {
            return writer.closed;
        },
        start: writer.start,
        save(page: number, count: number) {
            position({ page_number: page, page_count: count, updated_at: 0 });
            writer.save({ page_number: page, page_count: count });
        },
        close: writer.close,
    };
}

function localPositions(scope: LocalScope): Record<string, PdfPosition> {
    const raw = localStorage.getItem("tgdrive.pdf.positions.v1:" + scope);
    if (!raw || raw.length > 128 * 1024) return {};
    try {
        const data: unknown = JSON.parse(raw);
        if (!data || typeof data !== "object" || Array.isArray(data)) return {};
        return Object.fromEntries(
            Object.entries(data)
                .filter(([id, value]) => {
                    try {
                        return (
                            /^[A-Za-z0-9_-]{1,128}$/.test(id) &&
                            position(value) !== null
                        );
                    } catch {
                        return false;
                    }
                })
                .sort(([, a], [, b]) => a.updated_at - b.updated_at)
                .slice(-LOCAL_LIMIT),
        );
    } catch {
        return {};
    }
}

export function readLocalPdfPosition(fileId: string, scope: LocalScope) {
    const rows = localPositions(scope);
    return Object.hasOwn(rows, fileId) ? rows[fileId] : null;
}

export function saveLocalPdfPosition(
    fileId: string,
    scope: LocalScope,
    page: number,
    count: number,
) {
    const rows = localPositions(scope);
    delete rows[fileId];
    const latest = position({
        page_number: page,
        page_count: count,
        updated_at: Date.now() / 1000,
    });
    localStorage.setItem(
        "tgdrive.pdf.positions.v1:" + scope,
        JSON.stringify({
            ...Object.fromEntries(
                Object.entries(rows).slice(-(LOCAL_LIMIT - 1)),
            ),
            [fileId]: latest,
        }),
    );
}

export function pdfResumePage(saved: PdfPosition | null, pageCount: number) {
    return saved && saved.page_count === pageCount ? saved.page_number : null;
}
