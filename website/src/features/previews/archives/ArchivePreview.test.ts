import { describe, expect, it } from "vitest";
import {
    archiveEntries,
    archiveFolder,
    type ArchiveEntry,
} from "./ArchivePreview";

const file = (name: string): ArchiveEntry => ({
    name,
    size: 7,
    directory: false,
    encrypted: false,
});

describe("ZIP contents", () => {
    it("builds immediate folders from implicit paths and preserves empty folders", () => {
        const entries = [
            file("nested/deeper/notes.txt"),
            file("nested/photo.jpg"),
            file("root.txt"),
            { ...file("empty/"), directory: true },
        ];
        expect(archiveFolder(entries, "").map((item) => item.name)).toEqual([
            "empty",
            "nested",
            "root.txt",
        ]);
        expect(
            archiveFolder(entries, "nested/").map((item) => item.name),
        ).toEqual(["deeper", "photo.jpg"]);
        expect(archiveFolder(entries, "empty/")).toEqual([]);
    });
    it("keeps encrypted and inert markup names as data", () => {
        const entries = [
            { ...file("<img src=x onerror=alert(1)>"), encrypted: true },
            file("__proto__/test.txt"),
        ];
        expect(archiveEntries({ kind: "archive", entries })).toEqual(entries);
        expect(archiveFolder(entries, "")).toHaveLength(2);
    });
    it.each(["../outside", "/absolute", "bad\\path", "a/./b", "bad\u0000name"])(
        "rejects invalid directory name %s",
        (name) => {
            expect(() =>
                archiveEntries({ kind: "archive", entries: [file(name)] }),
            ).toThrow("invalid directory entry");
        },
    );
    it("rejects malformed sizes and unbounded entry counts", () => {
        for (const size of [-1, NaN, Infinity, 1.5])
            expect(() =>
                archiveEntries({
                    kind: "archive",
                    entries: [{ ...file("a"), size }],
                }),
            ).toThrow();
        expect(() =>
            archiveEntries({
                kind: "archive",
                entries: Array(10001).fill(file("a")),
            }),
        ).toThrow();
    });
});
