import { describe, expect, it } from "vitest";
import {
    DeviceUploadConcurrency,
    INITIAL_DEVICE_UPLOAD_CONCURRENCY,
    MAX_DEVICE_UPLOAD_CONCURRENCY,
} from "./deviceUploadConcurrency";

/** Two long PATCH bodies make rate tests independent of file completion. */
function connection() {
    const controller = new DeviceUploadConcurrency();
    let now = 0;
    let sequence = 0;
    const chunks = new Map<string, number>();
    function active(count: number) {
        while (chunks.size < count) {
            const id = String(++sequence);
            chunks.set(id, 0);
            controller.chunkStarted(id, now, 1e9);
        }
        while (chunks.size > count) {
            const id = [...chunks.keys()].at(-1)!;
            controller.chunkFinished(id, now);
            chunks.delete(id);
        }
    }
    function tick(rate: number, backlog = true) {
        now += 1000;
        for (const [id, bytes] of chunks) {
            const next = bytes + rate / chunks.size;
            chunks.set(id, next);
            controller.chunkProgress(id, next, now);
        }
        controller.sample(now, backlog);
    }
    function window(rate: number, backlog = true) {
        for (let second = 0; second < 4; second++) tick(rate, backlog);
        return controller.limit;
    }
    active(2);
    return {
        controller,
        active,
        tick,
        window,
        get now() {
            return now;
        },
    };
}

describe("adaptive device upload concurrency", () => {
    it("starts two requests immediately and waits for sustained measurement before probing", () => {
        const link = connection();
        expect(INITIAL_DEVICE_UPLOAD_CONCURRENCY).toBe(2);
        expect(link.controller.limit).toBe(2);
        expect(link.window(2000)).toBe(2);
        expect(link.window(2000)).toBe(3);
    });

    it("keeps an extra sender only when aggregate throughput improves", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        expect(link.window(3000)).toBe(3);
        expect(link.window(3000)).toBe(3);
        // A successful probe still settles before another increase.
        expect(link.window(3000)).toBe(3);
        expect(link.window(3000)).toBe(3);
        expect(link.window(3000)).toBe(4);
        link.active(4);
        link.window(4000);
        link.window(4000);
        for (let index = 0; index < 15; index++) link.window(4000);
        expect(link.controller.limit).toBe(MAX_DEVICE_UPLOAD_CONCURRENCY);
        expect(link.controller.limit).toBe(4);
    });

    it("rolls back a probe that only divides the same bandwidth among more files", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        link.window(2000);
        expect(link.window(2000)).toBe(2);
        link.active(2);
        for (let index = 0; index < 7; index++)
            expect(link.window(2000)).toBe(2);
        expect(link.window(2000)).toBe(3);
    });

    it("rejects a marginal improvement below ten percent", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        link.window(2180);
        expect(link.window(2180)).toBe(2);
    });

    it("compares both probe windows rather than retaining a short throughput spike", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        link.window(3500);
        expect(link.window(500)).toBe(2);
    });

    it("requires a second degraded window before reducing admission", () => {
        const link = connection();
        link.window(2000);
        // Do not offer a second stable window that would start an upward probe.
        expect(link.window(1000)).toBe(2);
        expect(link.window(1000)).toBe(1);
        // Existing requests retain their progress and are not aborted/removed.
        expect(link.window(1000)).toBe(1);
    });

    it("does not downshift for one isolated slow window", () => {
        const link = connection();
        link.window(2000);
        link.window(1000);
        expect(link.window(2000)).toBe(3);
    });

    it("reduces immediately on transient pressure and recovers cautiously after cooldown", () => {
        const link = connection();
        link.controller.reportPressure(link.now);
        expect(link.controller.limit).toBe(1);
        link.active(1);
        for (let index = 0; index < 7; index++)
            expect(link.window(1000)).toBe(1);
        expect(link.window(1000)).toBe(2);
    });

    it("reduces an in-progress three-sender probe by one, not two", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        expect(link.controller.limit).toBe(3);
        link.controller.reportPressure(link.now);
        expect(link.controller.limit).toBe(2);
        for (let index = 0; index < 10; index++)
            link.controller.reportPressure(link.now);
        expect(link.controller.limit).toBe(1);
    });

    it("does not probe while the small batch is draining or preparation owns the slots", () => {
        const draining = connection();
        for (let index = 0; index < 10; index++)
            expect(draining.window(2000, false)).toBe(2);
        const preparing = connection();
        preparing.active(1);
        for (let index = 0; index < 10; index++)
            expect(preparing.window(1000)).toBe(2);
    });

    it("discards a probe when there is insufficient remaining device work to compare", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        expect(link.window(3000, false)).toBe(2);
    });

    it("downshifts an observed stalled payload after fifteen seconds, without waiting for completion", () => {
        const link = connection();
        link.tick(2000);
        for (let index = 0; index < 14; index++) link.tick(0);
        expect(link.controller.limit).toBe(2);
        link.tick(0);
        expect(link.controller.limit).toBe(1);
    });

    it("does not classify a transport without browser progress as a stalled payload", () => {
        const link = connection();
        for (let index = 0; index < 24; index++) link.tick(0);
        expect(link.controller.limit).toBe(2);
    });

    it("does not measure the server acknowledgement delay as device saturation or a stall", () => {
        const controller = new DeviceUploadConcurrency();
        controller.chunkStarted("a", 0, 1000);
        controller.chunkStarted("b", 0, 1000);
        controller.chunkProgress("a", 1000, 1000);
        controller.chunkProgress("b", 1000, 1000);
        for (let now = 2000; now <= 40000; now += 1000)
            expect(controller.sample(now, true)).toBe(2);
    });

    it("uses confirmed bytes as a fallback without counting browser-observed bytes twice", () => {
        const controller = new DeviceUploadConcurrency();
        for (let index = 0; index < 2; index++) {
            const at = index * 4000;
            controller.chunkStarted(`a${index}`, at, 10000);
            controller.chunkStarted(`b${index}`, at, 10000);
            controller.chunkProgress(`a${index}`, 6000, at + 3000);
            controller.chunkFinished(`a${index}`, at + 4000, 10000);
            controller.chunkFinished(`b${index}`, at + 4000, 10000);
            controller.sample(at + 4000, true);
        }
        expect(controller.limit).toBe(3);
        // Baseline is 5000 bytes/s, so a real 6000 bytes/s is a useful increase.
        for (let index = 0; index < 2; index++) {
            const at = 8000 + index * 4000;
            for (let part = 0; part < 3; part++)
                controller.chunkStarted(`${index}-${part}`, at, 8000);
            for (let part = 0; part < 3; part++)
                controller.chunkFinished(`${index}-${part}`, at + 4000, 8000);
            controller.sample(at + 4000, true);
        }
        expect(controller.limit).toBe(3);
    });

    it("ignores unknown, regressing and invalid chunk progress", () => {
        const controller = new DeviceUploadConcurrency();
        for (let index = 0; index < 2; index++) {
            const at = index * 4000;
            controller.chunkStarted(`a${index}`, at, 10000);
            controller.chunkStarted(`b${index}`, at, 10000);
            controller.chunkProgress(`a${index}`, 5000, at + 1000);
            controller.chunkProgress(`a${index}`, 3000, at + 2000);
            controller.chunkProgress(`a${index}`, Number.NaN, at + 2000);
            controller.chunkProgress(`a${index}`, -100, at + 2000);
            controller.chunkProgress("unknown", 1e12, at + 2000);
            controller.chunkFinished(`a${index}`, at + 4000, 10000);
            controller.chunkFinished(`b${index}`, at + 4000, 10000);
            controller.sample(at + 4000, true);
        }
        expect(controller.limit).toBe(3);
        // Invalid observations must not inflate the baseline used by the probe.
        for (let index = 0; index < 2; index++) {
            const at = 8000 + index * 4000;
            for (let part = 0; part < 3; part++)
                controller.chunkStarted(`probe-${index}-${part}`, at, 8000);
            for (let part = 0; part < 3; part++)
                controller.chunkFinished(
                    `probe-${index}-${part}`,
                    at + 4000,
                    8000,
                );
            controller.sample(at + 4000, true);
        }
        expect(controller.limit).toBe(3);
    });

    it("drops long timer gaps instead of treating background-tab time as poor throughput", () => {
        const link = connection();
        link.window(2000);
        expect(link.controller.sample(link.now + 60000, true)).toBe(2);
        // Old progress/offsets cannot turn a resumed tab into an instantaneous probe.
        link.controller.chunkProgress("1", 1e8, link.now + 60001);
        expect(link.controller.sample(link.now + 61000, true)).toBe(2);
    });

    it("pauses hidden-tab measurements, retains a learned limit and resumes with a fresh window", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        link.window(3000);
        link.window(3000);
        expect(link.controller.limit).toBe(3);
        link.controller.pauseMeasurements(link.now);
        link.controller.chunkProgress("1", 1e8, link.now + 60000);
        expect(link.controller.sample(link.now + 60000, true)).toBe(3);
        for (let offset = 1000; offset <= 16000; offset += 1000)
            expect(
                link.controller.sample(link.now + 60000 + offset, true),
            ).toBe(3);
    });

    it("resets at true idle so the next batch starts at two", () => {
        const link = connection();
        link.controller.reportPressure(link.now);
        expect(link.controller.limit).toBe(1);
        link.controller.reset(50000);
        expect(link.controller.limit).toBe(2);
        // Progress from a completed earlier batch is ignored after reset.
        link.controller.chunkProgress("1", 1e9, 51000);
        expect(link.controller.sample(54000, true)).toBe(2);
    });

    it("discards a tentative fourth sender across suspension but preserves a measured fourth sender", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        for (let index = 0; index < 5; index++) link.window(3000);
        expect(link.controller.limit).toBe(4);
        link.controller.pauseMeasurements(link.now);
        expect(link.controller.limit).toBe(3);
        link.controller.sample(link.now, true);
        link.window(3000);
        expect(link.window(3000)).toBe(4);
        link.active(4);
        link.window(4000);
        link.window(4000);
        link.controller.pauseMeasurements(link.now);
        expect(link.controller.limit).toBe(4);
        expect(link.controller.sample(link.now + 60000, true)).toBe(4);
        expect(link.controller.sample(link.now + 120000, true)).toBe(4);
    });

    it("does not repeatedly penalize the same observed stall without new browser progress", () => {
        const link = connection();
        link.window(2000);
        link.window(2000);
        link.active(3);
        for (let index = 0; index < 5; index++) link.window(3000);
        link.active(4);
        link.window(4000);
        link.window(4000);
        for (let index = 0; index < 15; index++) link.tick(0);
        expect(link.controller.limit).toBe(3);
        for (let index = 0; index < 60; index++) link.tick(0);
        expect(link.controller.limit).toBe(3);
    });

    it("discards measurements after a backwards clock adjustment", () => {
        const link = connection();
        link.window(2000);
        expect(link.controller.sample(-1000, true)).toBe(2);
        // Returning to the prior clock cannot reuse its stable comparison window.
        expect(link.controller.sample(link.now, true)).toBe(2);
        expect(link.window(2000)).toBe(2);
    });
});
