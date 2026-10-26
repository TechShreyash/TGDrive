import type { DriveItem } from "../../shared/types";
import type { FileSort } from "./SortMenu";

export type SortOrder = "asc" | "desc";
export const defaultSortOrder = (sort: FileSort): SortOrder =>
    sort === "name" ? "asc" : "desc";
export function sortDriveItems(
    items: DriveItem[],
    sort: FileSort,
    order: SortOrder,
): DriveItem[] {
    const direction = order === "asc" ? 1 : -1;
    return items.slice().sort((a, b) => {
        if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
        const name = a.name.localeCompare(b.name, undefined, {
            numeric: true,
            sensitivity: "base",
        });
        const value =
            sort === "size"
                ? (a.size || 0) - (b.size || 0)
                : sort === "modified"
                  ? (Date.parse(a.upload_date || "") || 0) -
                    (Date.parse(b.upload_date || "") || 0)
                  : name;
        return value * direction || name || a.id.localeCompare(b.id);
    });
}
