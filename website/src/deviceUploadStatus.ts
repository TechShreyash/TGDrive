import type { Transfer } from "./transfers";

function isDeviceTransfer(item: Transfer): boolean {
  return (
    item.kind !== "remote" &&
    item.kind !== "delete" &&
    !item.historyOnly &&
    item.initiatedHere !== false &&
    !/^(server:|history:)/.test(item.batchId || "")
  );
}

/** A job ID confirms that the server owns the file and its remaining work. */
export function needsDeviceUpload(item: Transfer): boolean {
  return (
    isDeviceTransfer(item) &&
    !item.jobId &&
    item.state !== "completed" &&
    item.state !== "cancelled"
  );
}

export function deviceUploadMessage(
  items: Transfer[],
  otherDeviceUploadsPending = false,
): string | null {
  const deviceItems = items.filter(
    (item) => isDeviceTransfer(item) && item.state !== "cancelled",
  );
  // Finished receipts and server-discovered transfers need no device reminder.
  if (
    !deviceItems.length ||
    deviceItems.every((item) => item.state === "completed")
  )
    return null;

  const pending = deviceItems.filter(needsDeviceUpload).length;
  if (pending) {
    const received = deviceItems.length - pending;
    return (
      received +
      " of " +
      deviceItems.length +
      (deviceItems.length === 1 ? " file received" : " files received") +
      " by server · Keep this tab open until device uploads finish."
    );
  }

  if (otherDeviceUploadsPending)
    return "Files received by server. Keep this tab open for the remaining device uploads.";

  if (deviceItems.some((item) => item.state === "error"))
    return "Files received by server. You can close this tab; retry failed Telegram uploads from Transfers.";

  return "Device upload complete. You can close this tab; Telegram uploads continue on the server.";
}
