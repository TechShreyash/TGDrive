import { afterEach, describe, expect, it, vi } from "vitest";
import { loadPreview, nativePreviewKind } from "./Preview";

afterEach(() => vi.unstubAllGlobals());

describe("content-confirmed transport stream routing", () => {
    it.each(["async-await.ts", "lecture.TS", "module.mts", "module.cts"])(
        "asks the bounded descriptor before choosing a viewer for %s",
        (name) => {
            expect(nativePreviewKind(name)).toBeNull();
        },
    );

    it("accepts the additive content-confirmed video descriptor", async () => {
        const descriptor = { kind: "video", name: "actual.ts", size: 100956 };
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json(descriptor)),
        );
        await expect(
            loadPreview("/bound-file-id", "", new AbortController().signal),
        ).resolves.toEqual(descriptor);
    });

    it("retains inert TypeScript source when the descriptor says text", async () => {
        const descriptor = {
            kind: "text",
            text: "export const video = '<script>safe</script>';",
            language: "typescript",
            size: 50,
        };
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json(descriptor)),
        );
        await expect(
            loadPreview("/bound-file-id", "", new AbortController().signal),
        ).resolves.toEqual(descriptor);
    });

    it("routes SVGs to the passive image viewer", () => {
        expect(nativePreviewKind("drawing.svg")).toBe("image");
        expect(nativePreviewKind("drawing.SVG")).toBe("image");
        expect(nativePreviewKind("movie.m2ts")).toBe("video");
    });

    it("rejects unrecognized descriptor viewers", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ kind: "html", size: 12 })),
        );
        await expect(
            loadPreview("/bound-file-id", "", new AbortController().signal),
        ).rejects.toThrow("invalid preview");
    });
});
