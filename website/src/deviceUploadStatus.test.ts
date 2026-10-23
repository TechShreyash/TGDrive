import { describe, expect, it } from "vitest";
import { deviceUploadMessage, needsDeviceUpload } from "./deviceUploadStatus";
import type { Transfer } from "./transfers";

const local = (change: Partial<Transfer> = {}): Transfer => ({
  key: "device-file",
  name: "Photo.jpg",
  size: 100,
  total: 100,
  current: 0,
  lastModified: 0,
  destination: "/",
  initiatedHere: true,
  state: "queued",
  phase: "Waiting to send from this device",
  ...change,
});

describe("device upload readiness", () => {
  it("requires server acceptance even when every byte has been sent", () => {
    for (const state of [
      "queued",
      "verifying",
      "receiving",
      "uploading",
    ] as const) {
      const item = local({ state, current: 100, sentCurrent: 100 });
      expect(needsDeviceUpload(item)).toBe(true);
      expect(deviceUploadMessage([item])).toContain("0 of 1 file received");
      expect(deviceUploadMessage([item])).toContain("Keep this tab open");
      expect(deviceUploadMessage([item])).not.toContain("You can close");
    }
  });

  it("releases device dependence only after a job exists or the transfer ended", () => {
    expect(
      needsDeviceUpload(local({ jobId: "accepted", state: "uploading" })),
    ).toBe(false);
    expect(needsDeviceUpload(local({ state: "completed" }))).toBe(false);
    expect(needsDeviceUpload(local({ state: "cancelled" }))).toBe(false);
    expect(needsDeviceUpload(local({ state: "cancelling" }))).toBe(true);
    expect(
      deviceUploadMessage([local({ jobId: "accepted", state: "uploading" })]),
    ).toBe(
      "Device upload complete. You can close this tab; Telegram uploads continue on the server.",
    );
  });

  it("retains device dependence for missing originals and local upload failures", () => {
    for (const state of ["error", "awaiting_file"] as const) {
      expect(needsDeviceUpload(local({ state }))).toBe(true);
      expect(needsDeviceUpload(local({ state, jobId: "accepted" }))).toBe(
        false,
      );
      expect(deviceUploadMessage([local({ state })])).toContain(
        "Keep this tab open",
      );
    }
    expect(
      deviceUploadMessage([local({ state: "error", jobId: "accepted" })]),
    ).toBe(
      "Files received by server. You can close this tab; retry failed Telegram uploads from Transfers.",
    );
  });

  it("counts only uncancelled device files and confirms a whole batch together", () => {
    const received = local({ jobId: "accepted", state: "uploading" });
    const queued = local({ key: "queued" });
    const completed = local({ key: "finished", state: "completed" });
    const cancelled = local({ key: "cancelled", state: "cancelled" });
    expect(
      deviceUploadMessage([received, queued, completed, cancelled]),
    ).toContain("2 of 3 files received by server");
    expect(deviceUploadMessage([received, completed, cancelled])).toContain(
      "You can close this tab",
    );
    expect(deviceUploadMessage([completed, cancelled])).toBeNull();
    expect(deviceUploadMessage([])).toBeNull();
  });

  it("does not treat URL uploads or server history as device transfers", () => {
    for (const change of [
      { kind: "remote" },
      { kind: "delete" },
      { historyOnly: true },
      { initiatedHere: false },
      { batchId: "server:imported" },
      { batchId: "history:imported" },
    ]) {
      const item = local(change);
      expect(needsDeviceUpload(item)).toBe(false);
      expect(deviceUploadMessage([item])).toBeNull();
    }
  });

  it("does not offer to close the tab while another batch still needs the device", () => {
    const received = local({ jobId: "accepted", state: "uploading" });
    expect(deviceUploadMessage([received], true)).toBe(
      "Files received by server. Keep this tab open for the remaining device uploads.",
    );
    expect(deviceUploadMessage([received], true)).not.toContain(
      "You can close",
    );
  });
});
