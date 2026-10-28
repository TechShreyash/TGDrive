import { request } from "../../shared/api";
import type { DriveItem, Job } from "../../shared/types";

export function isZipArchive(item: Pick<DriveItem, "name" | "type">): boolean {
    return item.type === "file" && /\.zip$/i.test(item.name);
}

export function extractionFolderName(name: string): string {
    return name.replace(/\.zip$/i, "") || "Extracted files";
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
