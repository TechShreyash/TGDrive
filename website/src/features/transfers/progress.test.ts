import { describe, expect, it } from "vitest";
import {
    sampleProgress,
    currentRate,
    formatDuration,
    formatProgressPercent,
    transferDetails,
    transferProgress,
} from "./progress";
describe("truthful stage timing", () => {
    it("shows measured progress below one percent without rounding it up", () => {
        expect(formatProgressPercent(16 / (1.62 * 1024))).toBe("0.9%");
        expect(formatProgressPercent(0.00001)).toBe("<0.1%");
        expect(formatProgressPercent(0)).toBe("0%");
        expect(formatProgressPercent(0.25)).toBe("25%");
    });
    it("smooths acknowledged byte samples and expires stalled speed", () => {
        const first = sampleProgress(undefined, "receipt", 0, 1000);
        const second = sampleProgress(first, "receipt", 1000, 2000);
        expect(currentRate(second, 2500)).toBe(1000);
        const third = sampleProgress(second, "receipt", 3000, 3000);
        expect(currentRate(third, 3000)).toBeGreaterThan(1000);
        expect(currentRate(third, 19000)).toBeUndefined();
    });
    it("never carries throughput across stages, counter resets or restart gaps", () => {
        const sample = sampleProgress(
            sampleProgress(undefined, "download", 0, 1000),
            "download",
            100,
            2000,
        );
        expect(sampleProgress(sample, "upload", 0, 3000).rate).toBeUndefined();
        expect(
            sampleProgress(sample, "download", 0, 3000).rate,
        ).toBeUndefined();
        expect(
            sampleProgress(sample, "download", 200, 200000).rate,
        ).toBeUndefined();
    });
    it("shows no fabricated ETA for unknown totals or server waits", () => {
        const item = {
            state: "uploading",
            phase: "Downloading from URL",
            stage: "downloading",
            current: 40,
            total: 0,
            speedBytesPerSecond: 20,
            serverObservedAt: 1000,
        };
        expect(transferDetails(item, 2000)).toMatchObject({
            rate: 20,
            eta: undefined,
        });
        expect(
            transferDetails(
                { ...item, stage: "retry_wait", total: 100, etaSeconds: 3 },
                2000,
            ).rate,
        ).toBeUndefined();
        expect(
            transferDetails(
                {
                    ...item,
                    total: 100,
                    etaSeconds: 3,
                    error: "Connection lost",
                },
                2000,
            ).eta,
        ).toBeUndefined();
        expect(
            transferDetails({ ...item, total: 100, etaSeconds: 3 }, 17000).eta,
        ).toBeUndefined();
    });
    it("calculates a stage estimate from fresh measured speed when the server omits ETA", () => {
        const item = {
            state: "uploading",
            phase: "Sending to Telegram",
            stage: "uploading",
            current: 40,
            total: 100,
            speedBytesPerSecond: 20,
            serverObservedAt: 2000,
            progressUpdatedAt: 2,
        };
        expect(transferDetails(item, 2100)).toMatchObject({ rate: 20, eta: 3 });
        expect(transferDetails({ ...item, etaSeconds: NaN }, 2100).eta).toBe(3);
        expect(
            transferDetails({ ...item, speedBytesPerSecond: -20 }, 2100),
        ).toMatchObject({
            rate: undefined,
            eta: undefined,
            timing: "calculating",
        });
    });
    it("distinguishes initial measurements, stalls, provider waits and stage completion", () => {
        const item = {
            state: "uploading",
            phase: "Downloading from URL",
            stage: "downloading",
            current: 20,
            total: 100,
            serverObservedAt: 2000,
            progressUpdatedAt: 2,
        };
        expect(transferDetails(item, 2100).timing).toBe("calculating");
        expect(transferDetails(item, 18000)).toMatchObject({
            rate: undefined,
            eta: undefined,
            timing: "stalled",
        });
        expect(
            transferDetails({ ...item, stage: "retry_wait" }, 2100).timing,
        ).toBe("waiting");
        expect(
            transferDetails({ ...item, stage: "finalizing" }, 2100).timing,
        ).toBe("finishing");
        expect(transferDetails({ ...item, current: 100 }, 2100).timing).toBe(
            "finishing",
        );
        expect(
            transferDetails(
                {
                    ...item,
                    state: "receiving",
                    meter: {
                        stage: "receiving",
                        bytes: 20,
                        at: 1000,
                        advancedAt: 1000,
                        rate: 20,
                    },
                },
                18000,
            ).timing,
        ).toBe("stalled");
    });
    it("keeps elapsed time through reloads and freezes terminal durations", () => {
        const item = {
            state: "completed",
            phase: "Available",
            current: 100,
            total: 100,
            createdAt: 1000,
            startedAt: 2000,
            finishedAt: 5000,
        };
        expect(transferDetails(item, 99000).elapsed).toBe(3);
        expect(
            transferDetails(
                {
                    ...item,
                    kind: "delete",
                    state: "uploading",
                    stage: "uploading",
                    speedBytesPerSecond: 30,
                    serverObservedAt: 99000,
                },
                99000,
            ).rate,
        ).toBeUndefined();
        expect(formatDuration(3661)).toBe("1h 1m");
    });
});

it("does not show queue age or stale server-queue speed as work", () => {
    const queued = {
        state: "queued",
        phase: "Waiting",
        current: 0,
        total: 100,
        createdAt: 1000,
    };
    expect(transferDetails(queued, 120000)).toMatchObject({
        elapsed: undefined,
        rate: undefined,
        eta: undefined,
        measuring: false,
    });
    expect(
        transferDetails(
            {
                ...queued,
                state: "uploading",
                stage: "uploading",
                serverStatus: "queued",
                speedBytesPerSecond: 50,
                etaSeconds: 2,
                serverObservedAt: 120000,
            },
            120001,
        ),
    ).toMatchObject({ rate: undefined, eta: undefined, measuring: false });
    expect(
        transferDetails(
            {
                ...queued,
                state: "receiving",
                startedAt: 119000,
            },
            120000,
        ).elapsed,
    ).toBe(1);
});

describe("stage-local byte and percentage display", () => {
    const item = {
        state: "receiving",
        phase: "Sending from your device",
        current: 100,
        total: 100,
    };
    it("starts Telegram at zero after finishing the device transfer", () => {
        expect(transferProgress(item)).toMatchObject({
            current: 100,
            fraction: 0.99,
        });
        expect(
            transferProgress({
                ...item,
                state: "uploading",
                stage: "uploading",
                current: 0,
            }),
        ).toMatchObject({ current: 0, fraction: 0 });
        expect(
            transferProgress({
                ...item,
                state: "uploading",
                stage: "uploading",
                current: 38,
            }),
        ).toMatchObject({ current: 38, fraction: 0.38 });
        expect(transferProgress({ ...item, state: "completed" }).fraction).toBe(
            1,
        );
    });
    it("bounds invalid counts and never displays old progress for a queued job", () => {
        expect(
            transferProgress({
                ...item,
                state: "uploading",
                stage: "uploading",
                serverStatus: "queued",
            }),
        ).toMatchObject({ current: 0, fraction: 0 });
        expect(transferProgress({ ...item, current: Infinity }).current).toBe(
            0,
        );
        expect(transferProgress({ ...item, current: -1 }).current).toBe(0);
        expect(
            transferProgress({ ...item, total: 0 }).fraction,
        ).toBeUndefined();
        expect(
            transferProgress({ ...item, total: Infinity }).fraction,
        ).toBeUndefined();
    });
    it("shows measured in-flight bytes without treating them as a saved resume offset", () => {
        const observed = { ...item, current: 20, sentCurrent: 45 };
        expect(transferProgress(observed)).toMatchObject({
            current: 45,
            fraction: 0.45,
        });
        expect(transferProgress({ ...observed, state: "error" }).current).toBe(
            20,
        );
        expect(
            transferProgress({ ...observed, state: "uploading" }).current,
        ).toBe(20);
        expect(
            transferDetails({ ...observed, stage: "confirming" }, 1000),
        ).toMatchObject({ rate: undefined, eta: undefined });
    });
});

it("keeps acknowledged upload progress during local verification without reporting hashing speed", () => {
    const item = {
        state: "verifying",
        phase: "Checking the original file before resuming",
        current: 87,
        total: 100,
        meter: {
            stage: "old",
            bytes: 87,
            at: 1000,
            advancedAt: 1000,
            rate: 268000000,
        },
        speedBytesPerSecond: 268000000,
        etaSeconds: 1,
        serverObservedAt: 1000,
    };
    expect(transferProgress(item)).toMatchObject({
        current: 87,
        total: 100,
        fraction: 0.87,
    });
    expect(transferDetails(item, 1100)).toMatchObject({
        rate: undefined,
        eta: undefined,
        measuring: false,
    });
    const baseline = sampleProgress(undefined, "receiving", 87, 2000);
    expect(baseline.rate).toBeUndefined();
    const resumed = sampleProgress(baseline, "receiving", 97, 3000);
    expect(resumed.rate).toBe(10); // Only new bytes count toward transfer speed.
});
