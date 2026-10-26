import { describe, expect, it } from "vitest";
import {
    deviceUploadMessage,
    deviceUploadProgress,
    needsDeviceUpload,
} from "./deviceUploadStatus";
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
            expect(deviceUploadProgress([item])).toMatchObject({
                received: 0,
                count: 1,
                pending: true,
                current: 100,
                fraction: 0.99,
            });
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
            deviceUploadMessage([
                local({ jobId: "accepted", state: "uploading" }),
            ]),
        ).toBe("You can close this tab");
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
        ).toBe("You can close this tab");
    });

    it("counts only uncancelled device files and confirms a whole batch together", () => {
        const received = local({ jobId: "accepted", state: "uploading" });
        const queued = local({ key: "queued" });
        const completed = local({ key: "finished", state: "completed" });
        const cancelled = local({ key: "cancelled", state: "cancelled" });
        expect(
            deviceUploadProgress([received, queued, completed, cancelled]),
        ).toMatchObject({ received: 2, count: 3, current: 200, total: 300 });
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
        ]) {
            const item = local(change);
            expect(needsDeviceUpload(item)).toBe(false);
            expect(deviceUploadMessage([item])).toBeNull();
        }
    });

    it("does not offer to close the tab while another batch still needs the device", () => {
        const received = local({ jobId: "accepted", state: "uploading" });
        expect(deviceUploadMessage([received], true)).toBe(
            "Keep this tab open for other uploads",
        );
        expect(deviceUploadMessage([received], true)).not.toContain(
            "You can close",
        );
    });

    it("uses live browser bytes and retains received bytes while Telegram progresses", () => {
        const sending = local({
            state: "receiving",
            current: 20,
            sentCurrent: 40,
        });
        const accepted = local({
            key: "accepted",
            jobId: "job",
            state: "uploading",
            current: 1,
        });
        expect(deviceUploadProgress([sending, accepted])).toMatchObject({
            pending: true,
            received: 1,
            current: 140,
            total: 200,
            fraction: 0.7,
        });
        expect(
            deviceUploadProgress([
                { ...sending, state: "verifying", sentCurrent: 90 },
            ]),
        ).toMatchObject({ current: 20 });
    });
});
