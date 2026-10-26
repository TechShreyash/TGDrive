import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { displayText, loadPreview, nativePreviewKind } from "./Preview";
import { fileUrl } from "../../shared/types";
const viewerLoaders = vi.hoisted(
    () => [] as Parameters<(typeof import("react"))["lazy"]>[0][],
);
vi.mock("react", async (importOriginal) => {
    const react = await importOriginal<typeof import("react")>();
    return {
        ...react,
        lazy(load: Parameters<typeof react.lazy>[0]) {
            viewerLoaders.push(load);
            return react.lazy(load);
        },
    };
});
vi.mock("./pdf/PdfPreview", () => {
    throw new Error("The PDF viewer chunk is unavailable.");
});
vi.mock("./books/BookPreview", () => {
    throw new Error("The book viewer chunk is unavailable.");
});
afterEach(() => vi.unstubAllGlobals());
describe("bounded safe previews", () => {
    it("keeps recovery controls available when a document viewer chunk rejects", async () => {
        expect(viewerLoaders).toHaveLength(2);
        for (const load of viewerLoaders) {
            const viewer = await load();
            const html = renderToStaticMarkup(createElement(viewer.default));
            expect(html).toContain("Viewer could not load");
            expect(html).toContain("Reload Viewer");
            expect(html).toContain("download the original");
        }
    });
    it("formats valid JSON while preserving incomplete JSON and markup as text", () => {
        expect(displayText('{"ok":true}', "json")).toBe('{\n  "ok": true\n}');
        expect(displayText('{"incomplete":', "json")).toBe('{"incomplete":');
        expect(displayText("<script>alert(1)</script>", "html")).toBe(
            "<script>alert(1)</script>",
        );
        expect(nativePreviewKind("drawing.svg")).toBe("image");
        expect(nativePreviewKind("clip.MP4")).toBe("video");
    });
    it("keeps original download policy separate from preview/playback", () => {
        const url = new URL(
            fileUrl("/folder/id", "scoped token", true),
            "https://drive.test",
        );
        expect(url.searchParams.get("download")).toBe("1");
        expect(url.searchParams.get("auth")).toBe("scoped token");
        expect(url.searchParams.has("quality")).toBe(false);
        expect(fileUrl("/folder/id")).not.toContain("download");
    });
    it("passes scoped authorization and cancellation to the descriptor request", async () => {
        const fetcher = vi.fn(async () =>
            Response.json({
                kind: "text",
                text: "safe",
                size: 4,
                language: "text",
            }),
        );
        vi.stubGlobal("fetch", fetcher);
        const controller = new AbortController();
        expect(
            await loadPreview("/id", "share secret", controller.signal),
        ).toMatchObject({ text: "safe" });
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(
            new URL(url, "https://drive.test").searchParams.get("auth"),
        ).toBe("share secret");
        expect(options.credentials).toBe("same-origin");
        expect(options.signal).toBeInstanceOf(AbortSignal);
        controller.abort();
        expect(options.signal?.aborted).toBe(true);
    });
    it("rejects oversized and malformed responses instead of rendering them", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("x".repeat(2 * 1024 * 1024 + 1))),
        );
        await expect(
            loadPreview("/id", "", new AbortController().signal),
        ).rejects.toThrow("too large");
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ kind: "text", text: 17 })),
        );
        await expect(
            loadPreview("/id", "", new AbortController().signal),
        ).rejects.toThrow("invalid preview");
    });
    it.each([
        { kind: "unsupported", size: -1 },
        { kind: "unsupported", size: "10" },
        { kind: "unsupported", size: 10, reason: { text: "untrusted object" } },
        { kind: "unsupported", size: 10, reason: "x".repeat(2001) },
        { kind: "text", text: "safe", size: 4, language: {} },
        { kind: "text", text: "safe", size: 4, truncated: "yes" },
    ])("rejects invalid optional descriptor fields: %j", async (data) => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json(data)),
        );
        await expect(
            loadPreview("/id", "", new AbortController().signal),
        ).rejects.toThrow("invalid preview");
    });
    it("preserves a zero-byte unsupported file and its plain-text explanation", async () => {
        const data = {
            kind: "unsupported",
            size: 0,
            reason: "This format does not have a browser preview.",
        };
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json(data)),
        );
        await expect(
            loadPreview("/id", "", new AbortController().signal),
        ).resolves.toEqual(data);
    });
    it("preserves actionable authorization and backend errors", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    { detail: "This link has expired." },
                    { status: 403 },
                ),
            ),
        );
        await expect(
            loadPreview("/id", "", new AbortController().signal),
        ).rejects.toThrow("expired");
    });
});
