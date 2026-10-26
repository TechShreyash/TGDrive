import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TransferMetrics } from "./TransferPanel";
import type { Transfer } from "./transfers";

const item: Transfer = {
    key: "sample",
    initiatedHere: true,
    name: "Video.mp4",
    size: 1024 * 1024,
    lastModified: 0,
    destination: "/",
    state: "uploading",
    phase: "Uploading to Telegram",
    stage: "uploading",
    current: 512 * 1024,
    total: 1024 * 1024,
    speedBytesPerSecond: 128 * 1024,
    serverObservedAt: 2000,
    progressUpdatedAt: 2,
};
function render(change: Partial<Transfer> = {}, now = 2100) {
    return renderToStaticMarkup(
        createElement(TransferMetrics, { item: { ...item, ...change }, now }),
    );
}
describe("visible transfer timing", () => {
    it("shows throughput and estimated remaining time together", () => {
        const html = render();
        expect(html).toContain("128 KB/s");
        expect(html).toContain("About 4s remaining");
        expect(html).toContain(
            'title="Estimated upload time for this stage. Verification and publishing may take longer."',
        );
        expect(html).not.toContain("Telegram");
        expect(html).not.toContain("Calculating");
        expect(render({ stage: "downloading" })).toContain("128 KB/s");
    });
    it("leaves unknown timing quiet and reports meaningful waits without fabricated speed", () => {
        const initial = render({ speedBytesPerSecond: null });
        expect(initial).not.toContain("Calculating speed");
        expect(initial).not.toContain("Estimating time");
        const stalled = render({}, 18000);
        expect(stalled).toContain("No recent progress");
        expect(stalled).not.toContain("Time estimate unavailable");
        expect(stalled).not.toContain("128 KB/s");
        expect(stalled).not.toContain("About");
        expect(render({ total: 0 })).not.toContain("Time remaining unknown");
        expect(render({ stage: "retry_wait" })).toContain(
            "Waiting for Telegram",
        );
        expect(render({ stage: "source_retry_wait" })).toContain(
            "Waiting to retry source",
        );
        expect(render({ stage: "source_retry_wait" })).not.toContain(
            "No recent progress",
        );
        expect(render({ stage: "finalizing" })).toContain(
            "Finishing this stage",
        );
    });
});
