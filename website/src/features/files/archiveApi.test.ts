import { afterEach, describe, expect, it, vi } from "vitest";
import {
    archiveApi,
    defaultArchiveName,
    extractionFolderName,
    isExtractableArchive,
} from "./archiveApi";
import type { DriveItem } from "../../shared/types";

const item: DriveItem = {
    id: "file",
    path: "/",
    name: "Photos",
    type: "folder",
};

afterEach(() => vi.unstubAllGlobals());

describe("archive actions", () => {
    it.each([
        "zip",
        "7z",
        "rar",
        "tar",
        "tar.gz",
        "tgz",
        "tar.bz2",
        "tbz",
        "tbz2",
        "tar.xz",
        "txz",
        "tar.zst",
        "tzst",
        "gz",
        "bz2",
        "xz",
        "zst",
    ])(
        "offers extraction for supported %s files, including uppercase extensions",
        (suffix) => {
            expect(
                isExtractableArchive({
                    name: "Photos." + suffix,
                    type: "file",
                }),
            ).toBe(true);
            expect(
                isExtractableArchive({
                    name: "Photos." + suffix.toUpperCase(),
                    type: "file",
                }),
            ).toBe(true);
            expect(
                isExtractableArchive({
                    name: "Photos." + suffix,
                    type: "folder",
                }),
            ).toBe(false);
        },
    );

    it.each(["Photos.zip.exe", "Photos.7z.001", "Photos.iso", "Photos.txt"])(
        "does not offer extraction for unsupported %s files",
        (name) => {
            expect(isExtractableArchive({ name, type: "file" })).toBe(false);
        },
    );

    it.each([
        ["Photos.ZIP", "Photos"],
        ["Photos.7z", "Photos"],
        ["Photos.rar", "Photos"],
        ["Photos.tar.gz", "Photos"],
        ["Photos.TAR.BZ2", "Photos"],
        ["Photos.tar.xz", "Photos"],
        ["Photos.tar.zst", "Photos"],
        ["Photos.tgz", "Photos"],
        ["Photos.tbz", "Photos"],
        ["Photos.tbz2", "Photos"],
        ["Photos.txz", "Photos"],
        ["Photos.tzst", "Photos"],
        ["notes.txt.gz", "notes.txt"],
        ["notes.txt.bz2", "notes.txt"],
        ["notes.txt.xz", "notes.txt"],
        ["notes.txt.zst", "notes.txt"],
        [".tar.gz", "Extracted files"],
        [".zip", "Extracted files"],
    ])("suggests %s extraction folder as %s", (name, expected) => {
        expect(extractionFolderName(name)).toBe(expected);
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
        const longName = "a".repeat(254) + "📁" + "tail.tar.gz";
        expect(extractionFolderName(longName)).toBe("a".repeat(254) + "📁");
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
