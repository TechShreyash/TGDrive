import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CollapsedTransferSummary, TransferMetrics } from "./TransferPanel";
import { BatchProgressTracker } from "./batchProgress";
import {
    deviceUploadMessage,
    deviceUploadProgress,
    needsDeviceUpload,
} from "./deviceUploadStatus";
import { transferDetails } from "./progress";
import { summarizeBatches } from "./transferBatches";
import { mergeHistory } from "./transferHistory";
import { fromJob, type Transfer } from "./transfers";
import type { Job } from "../../shared/types";

function job(change: Partial<Job> = {}): Job {
    return {
        id: "archive",
        name: "Photos.zip",
        destination: "/",
        kind: "archive",
        status: "running",
        phase: "archiving",
        current: 1024,
        total: 2048,
        ...change,
    };
}
function transfer(change: Partial<Transfer> = {}): Transfer {
    return {
        key: "archive",
        initiatedHere: true,
        name: "Photos.zip",
        lastModified: 0,
        size: 0,
        destination: "/",
        jobId: "archive",
        state: "uploading",
        phase: "Creating ZIP on the server",
        stage: "archiving",
        kind: "archive",
        current: 1024,
        total: 2048,
        ...change,
    };
}

describe("archive transfer presentation", () => {
    it("tracks the output folder and current stage size after a job advances", () => {
        const row = transfer({ destination: "/source", size: 100 });
        Object.assign(
            row,
            fromJob(
                job({
                    kind: "extract",
                    destination: "/source/extracted",
                    phase: "uploading",
                    total: 2048,
                }),
            ),
        );
        expect(row).toMatchObject({
            destination: "/source/extracted",
            size: 2048,
        });
    });

    it("names each archive stage and preserves operation completion/failure messages", () => {
        expect(fromJob(job()).phase).toBe("Creating ZIP on the server");
        expect(
            fromJob(job({ kind: "extract", phase: "downloading" })).phase,
        ).toBe("Reading archive from your drive");
        expect(
            fromJob(
                job({ kind: "extract", name: "Photos.7z", phase: "verifying" }),
            ).phase,
        ).toBe("Checking archive before saving");
        expect(
            fromJob(job({ kind: "extract", phase: "extracting" })).phase,
        ).toBe("Extracting files on the server");
        expect(
            fromJob(job({ kind: "extract", phase: "uploading" })).phase,
        ).toBe("Saving extracted files to Telegram");
        expect(fromJob(job({ status: "queued" })).phase).toBe(
            "Queued on server",
        );
        expect(fromJob(job({ status: "error" })).phase).toBe(
            "ZIP creation failed",
        );
        expect(
            fromJob(job({ kind: "extract", status: "interrupted" })).phase,
        ).toBe("Extraction failed");
        expect(
            mergeHistory([job({ status: "completed" })], [], "all")[0].phase,
        ).toBe("ZIP available in your drive");
        expect(
            mergeHistory(
                [job({ kind: "extract", status: "completed" })],
                [],
                "all",
            )[0].phase,
        ).toBe("Extracted files available in your drive");
        expect(fromJob(job({ status: "cancel_requested" })).phase).toBe(
            "Cancelling and cleaning up",
        );
    });

    it("keeps archive tasks out of device receipts and browser upload speed estimates", () => {
        for (const kind of ["archive", "extract"]) {
            const row = transfer({ kind });
            expect(needsDeviceUpload(row)).toBe(false);
            expect(deviceUploadProgress([row]).count).toBe(0);
            expect(deviceUploadMessage([row])).toBeNull();
            const tracker = new BatchProgressTracker();
            tracker.observe(summarizeBatches([row], 1000), 1000);
            tracker.observe(
                summarizeBatches([{ ...row, current: 2000 }], 2000),
                2000,
            );
            expect(tracker.snapshot().size).toBe(0);
            const html = renderToStaticMarkup(
                createElement(CollapsedTransferSummary, {
                    batches: summarizeBatches([row], 1000),
                    deviceMessage: null,
                }),
            );
            expect(html).toContain("1 running");
            expect(html).not.toContain("uploading");
        }
    });

    it("uses server byte metrics during ZIP creation and extraction without upload-specific timing copy", () => {
        for (const stage of ["archiving", "extracting"]) {
            const row = transfer({
                stage,
                speedBytesPerSecond: 256,
                serverObservedAt: 10000,
                progressUpdatedAt: 10,
            });
            expect(transferDetails(row, 11000)).toMatchObject({
                rate: 256,
                eta: 4,
            });
            const html = renderToStaticMarkup(
                createElement(TransferMetrics, { item: row, now: 11000 }),
            );
            expect(html).toContain("256 B/s");
            expect(html).toContain("About 4s remaining");
            expect(html).not.toContain("Estimated upload time");
        }
    });
});
