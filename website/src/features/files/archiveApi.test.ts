import { afterEach, describe, expect, it, vi } from "vitest";
import {
    archiveApi,
    defaultArchiveName,
    extractionFolderName,
    isZipArchive,
} from "./archiveApi";
import type { DriveItem } from "../../shared/types";

const item: DriveItem = {
    id: "file",
    path: "/",
    name: "Photos",
    type: "folder",
};

afterEach(() => vi.unstubAllGlobals());

describe("ZIP actions", () => {
    it("offers extraction only for ZIP files, including uppercase extensions", () => {
        expect(isZipArchive({ name: "Photos.ZIP", type: "file" })).toBe(true);
        expect(isZipArchive({ name: "Photos.zip", type: "folder" })).toBe(
            false,
        );
        expect(isZipArchive({ name: "Photos.zip.exe", type: "file" })).toBe(
            false,
        );
    });

    it("suggests bounded ZIP and extraction names without splitting Unicode", () => {
        expect(defaultArchiveName([item])).toBe("Photos.zip");
        expect(defaultArchiveName([item, { ...item, id: "other" }])).toBe(
            "Archive.zip",
        );
        expect(
            defaultArchiveName([{ ...item, name: "a".repeat(255) }]),
        ).toHaveLength(255);
        expect(
            defaultArchiveName([
                { ...item, name: "a".repeat(250) + "📁" + "b".repeat(10) },
            ]),
        ).toBe("a".repeat(250) + "📁.zip");
        expect(extractionFolderName("Photos.ZIP")).toBe("Photos");
        expect(extractionFolderName(".zip")).toBe("Extracted files");
    });

    it("sends the destination choice and explicit public-access consent", async () => {
        const fetch = vi.fn(async () =>
            Response.json({ id: "job", job: { id: "job" } }),
        );
        vi.stubGlobal("fetch", fetch);
        await archiveApi.create(["/a", "/b"], "/target", "Photos.zip", false);
        expect(fetch).toHaveBeenLastCalledWith(
            "/api/v1/archives",
            expect.objectContaining({
                body: JSON.stringify({
                    paths: ["/a", "/b"],
                    destination: "/target",
                    name: "Photos.zip",
                    allow_public_exposure: false,
                }),
            }),
        );
        await archiveApi.extract("/source", "/target", null, true);
        expect(fetch).toHaveBeenLastCalledWith(
            "/api/v1/archives/extract",
            expect.objectContaining({
                body: JSON.stringify({
                    path: "/source",
                    destination: "/target",
                    folder_name: null,
                    allow_public_exposure: true,
                }),
            }),
        );
        await archiveApi.extract("/source", "/target", "Photos", false);
        expect(fetch).toHaveBeenLastCalledWith(
            "/api/v1/archives/extract",
            expect.objectContaining({
                body: JSON.stringify({
                    path: "/source",
                    destination: "/target",
                    folder_name: "Photos",
                    allow_public_exposure: false,
                }),
            }),
        );
    });
});
