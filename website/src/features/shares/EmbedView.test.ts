import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Preview from "../previews/Preview";
import { loadEmbeddedShare } from "./EmbedView";

afterEach(() => vi.unstubAllGlobals());

describe("embedded shared items", () => {
    it.each([
        ["", "token"],
        ["/file-id", ""],
        ["/share_folder-id", ""],
    ])(
        "rejects incomplete links before requesting owner-accessible endpoints",
        async (path, auth) => {
            const fetch = vi.fn();
            vi.stubGlobal("fetch", fetch);
            await expect(
                loadEmbeddedShare(path, auth, new AbortController().signal),
            ).rejects.toThrow("incomplete");
            expect(fetch).not.toHaveBeenCalled();
        },
    );

    it("loads the first folder page through the share-scoped paged route", async () => {
        const page = { items: [], total: 0, offset: 0, next_cursor: null };
        const fetch = vi.fn(async () => Response.json(page));
        vi.stubGlobal("fetch", fetch);
        await expect(
            loadEmbeddedShare(
                "/share_parent-id/folder-id",
                "share-token",
                new AbortController().signal,
            ),
        ).resolves.toEqual({ page });
        expect(fetch).toHaveBeenCalledWith(
            "/api/v1/shared/nodes?parent_id=%2Ffolder-id&limit=200&auth=share-token",
            expect.objectContaining({ method: "GET" }),
        );
    });

    it("loads file metadata with an explicit share token and preserves access errors", async () => {
        const file = {
            id: "file-id",
            name: "Photo.png",
            path: "",
            type: "file",
        };
        const fetch = vi.fn(async () => Response.json(file));
        vi.stubGlobal("fetch", fetch);
        await expect(
            loadEmbeddedShare(
                "/file-id",
                "share-token",
                new AbortController().signal,
            ),
        ).resolves.toEqual({ file });
        expect(fetch).toHaveBeenCalledWith(
            "/api/v1/embed?path=%2Ffile-id&auth=share-token",
            expect.objectContaining({ method: "GET" }),
        );
        fetch.mockImplementation(async () =>
            Response.json({ detail: "Invalid share link" }, { status: 403 }),
        );
        await expect(
            loadEmbeddedShare(
                "/file-id",
                "expired-token",
                new AbortController().signal,
            ),
        ).rejects.toThrow("Invalid share link");
    });

    it("keeps standalone files visible while folder previews retain a close control", () => {
        const props = {
            item: {
                id: "file-id",
                name: "Photo.png",
                path: "",
                type: "file" as const,
            },
            auth: "share-token",
        };
        const standalone = renderToStaticMarkup(createElement(Preview, props));
        expect(standalone).toContain("preview-embedded");
        expect(standalone).not.toContain('aria-label="Close Preview"');
        expect(standalone).toContain("auth=share-token");
        const folderPreview = renderToStaticMarkup(
            createElement(Preview, { ...props, onClose: vi.fn() }),
        );
        expect(folderPreview).not.toContain("preview-embedded");
        expect(folderPreview).toContain('aria-label="Close Preview"');
    });
});
