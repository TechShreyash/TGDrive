import { describe, expect, it } from "vitest";
import { defaultSortOrder, sortDriveItems } from "./fileSorting";
import type { DriveItem } from "../../shared/types";
const items: DriveItem[] = [
    {
        id: "b",
        name: "Beta",
        type: "file",
        path: "/",
        size: 10,
        upload_date: "2026-10-20",
    },
    {
        id: "a",
        name: "Alpha",
        type: "file",
        path: "/",
        size: 20,
        upload_date: "2026-10-19",
    },
    { id: "folder", name: "Zulu", type: "folder", path: "/" },
    {
        id: "c",
        name: "Gamma",
        type: "file",
        path: "/",
        size: 10,
        upload_date: "2026-10-18",
    },
];
describe("file sorting", () => {
    it.each([
        ["name", "asc", ["folder", "a", "b", "c"]],
        ["name", "desc", ["folder", "c", "b", "a"]],
        ["size", "asc", ["folder", "b", "c", "a"]],
        ["size", "desc", ["folder", "a", "b", "c"]],
        ["modified", "asc", ["folder", "c", "a", "b"]],
        ["modified", "desc", ["folder", "b", "a", "c"]],
    ] as const)(
        "sorts %s %s with folders first and stable ties",
        (field, order, ids) => {
            expect(
                sortDriveItems(items, field, order).map((item) => item.id),
            ).toEqual(ids);
            expect(items[0].id).toBe("b");
        },
    );
    it("uses ID as a final tie breaker and keeps natural names", () => {
        const values = ["10", "2", "1"].map((id) => ({
            id,
            name: "File " + id,
            type: "file" as const,
            path: "/",
        }));
        expect(sortDriveItems(values, "name", "asc").map((x) => x.id)).toEqual([
            "1",
            "2",
            "10",
        ]);
        expect(
            sortDriveItems(
                [
                    { ...values[0], id: "b", name: "Same" },
                    { ...values[0], id: "a", name: "Same" },
                ],
                "size",
                "desc",
            ).map((x) => x.id),
        ).toEqual(["a", "b"]);
    });
    it("preserves established defaults", () => {
        expect(defaultSortOrder("name")).toBe("asc");
        expect(defaultSortOrder("size")).toBe("desc");
        expect(defaultSortOrder("modified")).toBe("desc");
    });
});
