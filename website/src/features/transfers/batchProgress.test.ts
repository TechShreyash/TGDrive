import { describe, expect, it } from "vitest";
import { BatchProgressTracker } from "./batchProgress";
import { currentRate } from "./progress";
import { summarizeBatches } from "./transferBatches";
import type { Transfer } from "./transfers";

function item(key: string, overrides: Partial<Transfer> = {}): Transfer {
    return {
        key,
        initiatedHere: true,
        name: key + ".bin",
        size: 100,
        total: 100,
        current: 0,
        lastModified: 0,
        destination: "/",
        batchId: "folder",
        folderUpload: {
            id: "folder",
            name: "Folder",
            root_path: "/folder",
            created_at: 1,
        },
        relativePath: "Folder/" + key + ".bin",
        state: "uploading",
        phase: "Queued for Telegram",
        stage: "queued",
        jobId: key,
        ...overrides,
    };
}

function observe(
    tracker: BatchProgressTracker,
    items: Transfer[],
    now: number,
) {
    tracker.observe(summarizeBatches(items, now), now);
    return summarizeBatches(items, now, tracker.snapshot())[0];
}

describe("batch throughput observations", () => {
    it("estimates small files that finish between polls without individual speed samples", () => {
        const tracker = new BatchProgressTracker();
        const rows = [item("one"), item("two"), item("three")];
        expect(observe(tracker, rows, 1000).displayEta).toBeUndefined();
        rows[0] = { ...rows[0], state: "completed", current: 100 };
        expect(observe(tracker, rows, 3000)).toMatchObject({
            displayRate: 50,
            displayEta: 4,
        });
        rows[1] = { ...rows[1], stage: "finalizing", current: 100 };
        expect(observe(tracker, rows, 5000)).toMatchObject({
            displayRate: 50,
            displayEta: 2,
        });
        // A short gap between files keeps measured throughput; it does not create bytes.
        expect(observe(tracker, rows, 6000).displayEta).toBe(2);
        expect(observe(tracker, rows, 20000).displayEta).toBeUndefined();
    });

    it("retains device observations across handoffs and starts a new Telegram baseline", () => {
        const tracker = new BatchProgressTracker();
        const rows = [
            item("one", {
                state: "receiving",
                stage: "preparing",
                jobId: undefined,
            }),
            item("two", {
                state: "queued",
                stage: undefined,
                jobId: undefined,
            }),
        ];
        observe(tracker, rows, 1000);
        rows[0] = item("one");
        rows[1] = { ...rows[1], state: "receiving", current: 50 };
        expect(observe(tracker, rows, 3000)).toMatchObject({
            device: { current: 150, pending: true },
            displayRate: 75,
            displayEta: 50 / 75,
        });
        rows[1] = item("two");
        expect(observe(tracker, rows, 4000).displayRate).toBeUndefined();
        rows[0] = { ...rows[0], state: "completed", current: 100 };
        expect(observe(tracker, rows, 6000)).toMatchObject({
            device: { pending: false },
            displayRate: 50,
            displayEta: 2,
        });
    });

    it("never derives a rate from the first restored snapshot or a wall clock tick", () => {
        const tracker = new BatchProgressTracker();
        const rows = [item("one", { current: 50, stage: "uploading" })];
        observe(tracker, rows, 1000);
        expect(observe(tracker, rows, 5000).displayRate).toBeUndefined();
        expect(
            currentRate(tracker.snapshot().get("folder"), 5000),
        ).toBeUndefined();
    });

    it.each(["membership", "total", "regression", "cancellation"])(
        "resets the baseline after %s changes",
        (change) => {
            const tracker = new BatchProgressTracker();
            let rows = [item("one", { stage: "uploading" }), item("two")];
            observe(tracker, rows, 1000);
            rows[0] = { ...rows[0], current: 50 };
            expect(observe(tracker, rows, 3000).displayRate).toBe(25);
            if (change === "membership") rows.push(item("three"));
            if (change === "total")
                rows[0] = { ...rows[0], size: 200, total: 200 };
            if (change === "regression") rows[0] = { ...rows[0], current: 0 };
            if (change === "cancellation")
                rows[1] = { ...rows[1], state: "cancelled" };
            expect(observe(tracker, rows, 4000).displayRate).toBeUndefined();
        },
    );

    it("drops observations for removed or finished batches", () => {
        const tracker = new BatchProgressTracker();
        observe(tracker, [item("one")], 1000);
        expect(tracker.snapshot().size).toBe(1);
        observe(tracker, [item("one", { state: "completed" })], 2000);
        expect(tracker.snapshot().size).toBe(0);
        tracker.observe([], 3000);
        expect(tracker.snapshot().size).toBe(0);
    });

    it("starts new measurements after suspension without reviving the old rate", () => {
        const tracker = new BatchProgressTracker();
        const rows = [item("one", { stage: "uploading" })];
        observe(tracker, rows, 1000);
        rows[0].current = 50;
        expect(observe(tracker, rows, 3000).displayRate).toBe(25);
        tracker.clear();
        expect(tracker.snapshot().size).toBe(0);
        expect(observe(tracker, rows, 4000).displayRate).toBeUndefined();
    });

    it("does not turn a recent observation into speed during publishing or retry waits", () => {
        const tracker = new BatchProgressTracker();
        const rows = [item("one", { stage: "uploading" })];
        observe(tracker, rows, 1000);
        rows[0].current = 50;
        expect(observe(tracker, rows, 3000).displayRate).toBe(25);
        rows[0].stage = "retry_wait";
        expect(observe(tracker, rows, 4000)).toMatchObject({
            displayRate: undefined,
            displayEta: undefined,
        });
        rows[0] = { ...rows[0], stage: "finalizing", current: 100 };
        expect(observe(tracker, rows, 5000)).toMatchObject({
            displayRate: undefined,
            displayEta: undefined,
        });
    });
});
