import { request } from "../../shared/api";
import type { DriveItem, Job } from "../../shared/types";

const extractionSuffix =
    /\.(?:tar\.(?:gz|bz2|xz|zst)|zip|7z|rar|tar|tgz|tbz2?|txz|tzst|gz|bz2|xz|zst)$/i;

export function isExtractableArchive(
    item: Pick<DriveItem, "name" | "type">,
): boolean {
    return item.type === "file" && extractionSuffix.test(item.name);
}

export function extractionFolderName(name: string): string {
    return (
        Array.from(name.replace(extractionSuffix, "")).slice(0, 255).join("") ||
        "Extracted files"
    );
}

export function defaultArchiveName(items: DriveItem[]): string {
    const base =
        items.length === 1
            ? Array.from(items[0].name).slice(0, 251).join("")
            : "Archive";
    return base + ".zip";
}

export const archiveApi = {
    create: (
        paths: string[],
        destination: string,
        name: string,
        allow_public_exposure: boolean,
    ) =>
        request<{ id: string; job: Job }>("/api/v1/archives", {
            paths,
            destination,
            name,
            allow_public_exposure,
        }),
    extract: (
        path: string,
        destination: string,
        folder_name: string | null,
        allow_public_exposure: boolean,
    ) =>
        request<{ id: string; job: Job }>("/api/v1/archives/extract", {
            path,
            destination,
            folder_name,
            allow_public_exposure,
        }),
};
