import type { Transfer } from "./transfers";

export function isServerImport(item: Pick<Transfer, "kind">): boolean {
    return item.kind === "remote" || item.kind === "torrent";
}

export function isArchiveTransfer(item: Pick<Transfer, "kind">): boolean {
    return item.kind === "archive" || item.kind === "extract";
}

/** Folder selections are one task; ordinary files each keep their own identity. */
export function isFolderTransfer(item: Transfer): boolean {
    return Boolean(item.folderUpload);
}

export function transferTaskId(item: Transfer): string {
    return item.folderUpload?.id || item.key;
}

/** The final catalog id lets the server resolve a folder after it moves. */
export function transferFolderDestination(item: Transfer): string {
    return item.folderUpload?.root_path || item.destination;
}
