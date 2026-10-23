import type { Transfer } from "./transfers";

/** Folder selections are one task; ordinary files each keep their own identity. */
export function isFolderTransfer(item: Transfer): boolean {
  return Boolean(item.folderUpload || item.relativePath);
}

export function transferTaskId(item: Transfer): string {
  if (item.folderUpload) return item.folderUpload.id;
  // Older browser recovery records identified folders by rooted file paths.
  if (item.relativePath)
    return item.batchId || "legacy:" + (item.jobId || item.key);
  // Ignore historical multi-file selection batchIds: they were never folders.
  return item.key;
}

/** The final catalog id lets the server resolve a folder after it moves. */
export function transferFolderDestination(item: Transfer): string {
  if (item.folderUpload) return item.folderUpload.root_path;
  let destination = item.destination;
  const depth = item.relativePath
    ? Math.max(0, item.relativePath.split("/").length - 2)
    : 0;
  for (let index = 0; index < depth; index++)
    destination = destination.replace(/\/?[^/]+$/, "") || "/";
  return destination;
}
