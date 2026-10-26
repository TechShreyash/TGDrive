import { describe, expect, it } from "vitest";
import type { Transfer } from "./transfers";
import { summarizeBatches, batchCategory } from "./transferBatches";

function item(overrides: Partial<Transfer> = {}): Transfer {
    const relativePath = Object.hasOwn(overrides, "relativePath")
        ? overrides.relativePath
        : "My folder/one.bin";
    const batchId = Object.hasOwn(overrides, "batchId")
        ? overrides.batchId
        : "batch";
    return {
        key: "one",
        initiatedHere: true,
        name: "one.bin",
        size: 100,
        lastModified: 0,
        destination: "/",
        batchId: "batch",
        batchName: "My folder",
        relativePath: "My folder/one.bin",
        folderUpload:
            relativePath && batchId
                ? {
                      id: batchId,
                      name: overrides.batchName || "My folder",
                      root_path: "/folder",
                      created_at: 1,
                  }
                : undefined,
        batchCreatedAt: 1000,
        state: "queued",
        phase: "Waiting",
        current: 0,
        total: 100,
        ...overrides,
    };
}
function summary(items: Transfer[], now = 10000) {
    return summarizeBatches(items, now)[0];
}
const rate = {
    serverObservedAt: 9000,
    progressUpdatedAt: 9,
    speedBytesPerSecond: 10,
};

describe("truthful batch work", () => {
    it("weights file bytes instead of averaging file percentages", () => {
        const batch = summary([
            item({
                size: 900,
                total: 900,
                state: "completed",
                finishedAt: 8000,
            }),
            item({ key: "small", size: 100, state: "queued" }),
        ]);
        expect(batch.totalBytes).toBe(1000);
        expect(batch.completedBytes).toBe(900);
        expect(batch.progress).toBe(0.9);
        expect(batch).toMatchObject({
            count: 2,
            completed: 1,
            queued: 1,
            active: 0,
            pending: true,
        });
    });
    it("uses the actual stage counter and resets for the Telegram queue", () => {
        expect(
            summary([item({ state: "receiving", current: 100 })]).progress,
        ).toBe(0.99);
        expect(
            summary([item({ state: "verifying", current: 50, total: 50 })])
                .progress,
        ).toBe(0.5);
        expect(
            summary([item({ state: "awaiting_file", current: 1000 })]).progress,
        ).toBe(0.99);
        expect(
            summary([
                item({
                    state: "uploading",
                    jobId: "job",
                    stage: "queued",
                    current: 100,
                }),
            ]).progress,
        ).toBe(0);
        expect(
            summary([
                item({
                    state: "uploading",
                    jobId: "job",
                    stage: "uploading",
                    current: 40,
                }),
            ]).progress,
        ).toBe(0.4);
    });
    it("resets to the new stage counter and never treats finalization as complete", () => {
        const receiving = item({ state: "receiving", current: 100 });
        expect(summary([receiving]).progress).toBe(0.99);
        const sending = {
            ...receiving,
            state: "uploading" as const,
            jobId: "job",
            stage: "uploading",
            current: 0,
        };
        expect(summary([sending]).progress).toBe(0);
        expect(
            summary([{ ...sending, current: 100, stage: "finalizing" }])
                .progress,
        ).toBe(0.99);
        expect(summary([{ ...sending, state: "completed" }]).progress).toBe(1);
    });
    it("bounds invalid counters and suppresses percentages for unknown sizes", () => {
        expect(
            summary([item({ state: "receiving", current: -20 })]).progress,
        ).toBe(0);
        expect(
            summary([item({ state: "receiving", current: Infinity })]).progress,
        ).toBe(0);
        expect(summary([item({ size: 0, total: 0 })]).progress).toBeUndefined();
        expect(
            summary([item({ size: 0, total: 0 })]).totalBytes,
        ).toBeUndefined();
        expect(
            summary([item({ size: 0, total: 0, state: "completed" })]).progress,
        ).toBe(1);
        expect(
            summary([
                item({
                    size: 0,
                    total: 20,
                    jobId: "job",
                    state: "uploading",
                    current: 5,
                }),
            ]).progress,
        ).toBe(0.25);
    });
    it("never counts cancelled or failed files as confirmed completion", () => {
        const batch = summary([
            item({ state: "completed", finishedAt: 7000 }),
            item({
                key: "cancelled",
                state: "cancelled",
                jobId: "job",
                current: 100,
                finishedAt: 8000,
            }),
        ]);
        expect(batch).toMatchObject({
            completed: 1,
            cancelled: 1,
            completedBytes: 100,
            pending: false,
            progress: 0.5,
        });
        expect(
            summary([item({ state: "error", current: 100, jobId: "job" })]),
        ).toMatchObject({
            failed: 1,
            pending: true,
            progress: 0.99,
            completedBytes: 0,
        });
    });
    it("keeps URL imports and deletions on their own stage units", () => {
        const remote = summary([
            item({
                kind: "remote",
                state: "uploading",
                stage: "downloading",
                size: 0,
                current: 25,
                total: 100,
                jobId: "job",
            }),
        ]);
        expect(remote.progress).toBe(0.25);
        expect(remote.totalBytes).toBe(100);
        expect(
            summary([item({ kind: "remote", total: 0, size: 0 })]).progress,
        ).toBeUndefined();
        const deletion = summary([
            item({
                kind: "delete",
                jobId: "job",
                state: "uploading",
                current: 2,
                total: 4,
            }),
        ]);
        expect(deletion).toMatchObject({ progress: 0.5, completedBytes: 0 });
        expect(deletion.totalBytes).toBeUndefined();
        expect(
            summary([item({ kind: "delete", state: "completed" })]).progress,
        ).toBe(1);
        const mixed = summary([
            item({ state: "completed" }),
            item({
                key: "delete",
                kind: "delete",
                state: "uploading",
                current: 3,
                total: 5,
            }),
        ]);
        expect(mixed.progress).toBeUndefined();
        expect(mixed.totalBytes).toBeUndefined();
    });
});

describe("stage rates and batch ETA", () => {
    it("sums fresh device and Telegram rates independently", () => {
        const batch = summary([
            item({
                state: "receiving",
                meter: {
                    stage: "receipt",
                    bytes: 20,
                    at: 9000,
                    advancedAt: 9000,
                    rate: 20,
                },
            }),
            item({
                key: "server",
                state: "uploading",
                jobId: "job",
                stage: "uploading",
                current: 40,
                ...rate,
            }),
        ]);
        expect(batch.deviceRate).toBe(20);
        expect(batch.telegramRate).toBe(10);
        expect(batch.eta).toBeUndefined();
    });
    it("omits stale, failed, finalizing and URL-download rates", () => {
        expect(
            summary(
                [
                    item({
                        state: "receiving",
                        meter: {
                            stage: "receipt",
                            bytes: 20,
                            at: 1000,
                            advancedAt: 1000,
                            rate: 20,
                        },
                    }),
                ],
                17000,
            ).deviceRate,
        ).toBeUndefined();
        for (const change of [
            { serverObservedAt: 1000 },
            { progressUpdatedAt: 1 },
            { error: "Disconnected" },
            { stage: "retry_wait" },
            { stage: "finalizing" },
            { kind: "remote", stage: "downloading" },
        ]) {
            const batch = summary(
                [
                    item({
                        state: "uploading",
                        jobId: "job",
                        stage: "uploading",
                        ...rate,
                        ...change,
                    }),
                ],
                17000,
            );
            expect(batch.telegramRate).toBeUndefined();
            expect(batch.eta).toBeUndefined();
        }
    });
    it("estimates only known remaining server-side Telegram work", () => {
        const running = item({
            state: "uploading",
            jobId: "job",
            stage: "uploading",
            current: 40,
            ...rate,
        });
        const queued = item({
            key: "next",
            state: "uploading",
            jobId: "nextjob",
            stage: "queued",
            current: 80,
        });
        const batch = summary([running, queued]);
        expect(batch.telegramRate).toBe(10);
        expect(batch.eta).toBe(16);
        for (const change of [
            { state: "error" as const },
            { state: "awaiting_file" as const },
            { state: "cancelling" as const },
            { stage: "retry_wait" },
            { stage: "waiting_for_telegram" },
            { stage: "finalizing" },
            { size: 0, total: 0 },
            { jobId: undefined },
        ])
            expect(
                summary([running, { ...queued, ...change }]).eta,
            ).toBeUndefined();
        expect(summary([running, { ...queued, state: "cancelled" }]).eta).toBe(
            6,
        );
        expect(
            summary([
                running,
                { ...queued, state: "cancelled", size: 0, total: 0 },
            ]).eta,
        ).toBe(6);
    });

    it("allows byte-complete publishing siblings without estimating their publication wait", () => {
        const running = item({
            state: "uploading",
            jobId: "job",
            stage: "uploading",
            current: 40,
            ...rate,
        });
        for (const stage of ["finalizing", "backup_pending"]) {
            const publishing = item({
                key: "publishing",
                jobId: "publishing",
                state: "uploading",
                stage,
                current: 100,
            });
            expect(summary([running, publishing]).eta).toBe(6);
            expect(summary([publishing]).eta).toBeUndefined();
        }
    });

    it("uses fresh batch observations only as a fallback for the displayed stage", () => {
        const running = item({
            state: "uploading",
            jobId: "job",
            stage: "uploading",
            current: 40,
        });
        const meter = new Map([
            [
                "batch",
                {
                    stage: "telegram",
                    bytes: 40,
                    at: 9000,
                    advancedAt: 9000,
                    rate: 20,
                },
            ],
        ]);
        expect(summarizeBatches([running], 10000, meter)[0]).toMatchObject({
            telegramRate: 20,
            displayEta: 3,
        });
        expect(
            summarizeBatches([{ ...running, ...rate }], 10000, meter)[0],
        ).toMatchObject({
            telegramRate: 10,
            displayEta: 6,
        });
        expect(
            summarizeBatches([running], 24000, meter)[0].displayEta,
        ).toBeUndefined();
        meter.get("batch")!.stage = "device";
        expect(
            summarizeBatches([running], 10000, meter)[0].displayEta,
        ).toBeUndefined();
    });

    it("does not let a Telegram failure hide healthy pending device ETA", () => {
        const device = item({
            state: "receiving",
            current: 50,
            meter: {
                stage: "receiving",
                bytes: 50,
                at: 9000,
                advancedAt: 9000,
                rate: 10,
            },
        });
        const failed = item({
            key: "server-error",
            jobId: "accepted",
            state: "error",
            error: "Telegram failed",
        });
        expect(summary([device, failed]).displayEta).toBe(5);
        expect(
            summary([
                { ...device, state: "queued", stage: "retry_wait" },
                failed,
            ]).displayEta,
        ).toBeUndefined();
        expect(
            summary([{ ...device, state: "awaiting_file" }, failed]).displayEta,
        ).toBeUndefined();
    });
});

describe("batch identity, attention and elapsed time", () => {
    it("keeps each ordinary selection in its own file task", () => {
        const tasks = summarizeBatches(
            [
                item({
                    key: "first",
                    name: "first.bin",
                    relativePath: undefined,
                }),
                item({
                    key: "second",
                    name: "second.bin",
                    relativePath: undefined,
                }),
            ],
            10000,
        );
        expect(tasks).toHaveLength(2);
        expect(tasks.map((task) => task.name).sort()).toEqual([
            "first.bin",
            "second.bin",
        ]);
        expect(tasks.every((task) => task.count === 1 && !task.folder)).toBe(
            true,
        );
        expect(new Set(tasks.map((task) => task.id)).size).toBe(2);
    });
    it("uses persisted folder metadata across independently discovered server rows", () => {
        const folderUpload = {
            id: "folder-upload-identity",
            name: "Project files",
            root_path: "/project-root",
            created_at: 1,
        };
        const tasks = summarizeBatches(
            [
                item({
                    key: "server-one",
                    batchId: "history:one",
                    relativePath: undefined,
                    folderUpload,
                }),
                item({
                    key: "server-two",
                    batchId: "server:two",
                    relativePath: undefined,
                    folderUpload,
                }),
            ],
            10000,
        );
        expect(tasks).toHaveLength(1);
        expect(tasks[0]).toMatchObject({
            id: folderUpload.id,
            name: folderUpload.name,
            folder: true,
            count: 2,
        });
        expect(summary([tasks[0].items[0]])).toMatchObject({
            id: folderUpload.id,
            folder: true,
            count: 1,
        });
    });
    it("sorts attention, active and finished batches by newest within class", () => {
        const rows = [
            item({
                key: "done",
                batchId: "done",
                state: "completed",
                batchCreatedAt: 9000,
            }),
            item({
                key: "active-old",
                batchId: "active-old",
                batchCreatedAt: 2000,
            }),
            item({
                key: "error",
                batchId: "error",
                state: "error",
                batchCreatedAt: 3000,
            }),
            item({
                key: "await",
                batchId: "await",
                state: "awaiting_file",
                batchCreatedAt: 4000,
            }),
            item({
                key: "active-new",
                batchId: "active-new",
                batchCreatedAt: 5000,
            }),
        ];
        expect(summarizeBatches(rows, 10000).map((batch) => batch.id)).toEqual([
            "await",
            "error",
            "active-new",
            "active-old",
            "done",
        ]);
        expect(rows.map((row) => row.key)).toEqual([
            "done",
            "active-old",
            "error",
            "await",
            "active-new",
        ]);
    });
    it("sorts member attention before running, queued and terminal files", () => {
        const rows = [
            item({ key: "cancel", state: "cancelled" }),
            item({ key: "done", state: "completed" }),
            item({ key: "queue", state: "queued" }),
            item({ key: "active", state: "receiving" }),
            item({ key: "await", state: "awaiting_file" }),
            item({ key: "error", state: "error" }),
        ];
        const batch = summary(rows);
        expect(batch.items.map((row) => row.key)).toEqual([
            "error",
            "await",
            "active",
            "queue",
            "done",
            "cancel",
        ]);
        expect(batch).toMatchObject({
            count: 6,
            completed: 1,
            cancelled: 1,
            failed: 1,
            awaiting: 1,
            active: 1,
            queued: 1,
        });
    });
    it("uses stable local identities for standalone files", () => {
        expect(
            summarizeBatches(
                [
                    item({
                        batchId: undefined,
                        relativePath: undefined,
                        jobId: "server",
                    }),
                    item({
                        key: "local",
                        batchId: undefined,
                        relativePath: undefined,
                    }),
                ],
                10000,
            )
                .map((batch) => batch.id)
                .sort(),
        ).toEqual(["local", "one"]);
        expect(
            summary([
                item({
                    batchId: undefined,
                    batchName: undefined,
                    relativePath: undefined,
                }),
            ]).name,
        ).toBe("one.bin");
        expect(summarizeBatches([], 10000)).toEqual([]);
    });
    it("starts with actual work and freezes only fully settled batches", () => {
        const rows = [
            item({
                createdAt: 3000,
                startedAt: 4000,
                batchCreatedAt: 1000,
                state: "completed",
                finishedAt: 5000,
            }),
            item({
                key: "two",
                createdAt: 4000,
                startedAt: 5000,
                batchCreatedAt: 2000,
                state: "cancelled",
                finishedAt: 7000,
            }),
        ];
        expect(summary(rows, 90000).elapsed).toBe(3);
        expect(summary([{ ...rows[0], state: "error" }], 90000).elapsed).toBe(
            86,
        );
        expect(
            summary([item({ createdAt: undefined, batchCreatedAt: undefined })])
                .elapsed,
        ).toBeUndefined();
        expect(
            summary([item({ state: "completed", finishedAt: undefined })])
                .elapsed,
        ).toBeUndefined();
    });
});

it("recognizes server queued status without reusing the old counter or rate", () => {
    const queued = item({
        state: "uploading",
        jobId: "job",
        serverStatus: "queued",
        stage: "uploading",
        current: 100,
        ...rate,
    });
    expect(summary([queued])).toMatchObject({
        progress: 0,
        queued: 1,
        active: 0,
    });
    expect(summary([queued]).telegramRate).toBeUndefined();
    expect(summary([{ ...queued, kind: "remote" }]).progress).toBe(0);
});

it("keeps missing dates behind newer batches and emits no nonfinite ETA", () => {
    const missing = item({
        key: "missing",
        batchId: "missing",
        batchCreatedAt: undefined,
        createdAt: undefined,
    });
    const known = item({
        key: "known",
        batchId: "known",
        batchCreatedAt: 1000,
    });
    expect(
        summarizeBatches([missing, known], 10000).map((batch) => batch.id),
    ).toEqual(["known", "missing"]);
    expect(
        summary([
            item({
                state: "uploading",
                jobId: "job",
                stage: "uploading",
                ...rate,
                speedBytesPerSecond: 1e-320,
            }),
        ]).eta,
    ).toBeUndefined();
});

it("keeps queued-only batches free of elapsed time and ETA", () => {
    const queued = item({ createdAt: 1000, batchCreatedAt: 1000 });
    expect(summary([queued], 3600000)).toMatchObject({
        queued: 1,
        elapsed: undefined,
        deviceRate: undefined,
        telegramRate: undefined,
        eta: undefined,
    });
    const working = item({
        key: "started",
        state: "receiving",
        createdAt: 2000,
        startedAt: 9000,
    });
    expect(summary([queued, working], 10000).elapsed).toBe(1);
});

it("keeps Telegram accounting separate while device progress owns the visible bar", () => {
    const sending = item({
        key: "sending",
        state: "uploading",
        jobId: "job",
        stage: "uploading",
        current: 40,
    });
    const receiving = item({ key: "device", state: "receiving", current: 100 });
    const first = summary([sending, receiving]);
    expect(first).toMatchObject({
        uploadedBytes: 40,
        completedBytes: 0,
        totalBytes: 200,
        displayBytes: 200,
        progress: 0.99,
        device: { pending: true, received: 1, count: 2 },
    });
    expect(summary([{ ...sending, current: 60 }, receiving])).toMatchObject({
        uploadedBytes: 60,
        progress: 0.99,
    });
    expect(
        summary([{ ...sending, state: "completed" }, receiving]),
    ).toMatchObject({
        uploadedBytes: 100,
        completedBytes: 100,
        progress: 0.99,
    });
    expect(
        summary([{ ...sending, current: 100, stage: "queued" }, receiving]),
    ).toMatchObject({ uploadedBytes: 0, progress: 0.99 });
    expect(
        summary([
            sending,
            {
                ...receiving,
                jobId: "second",
                state: "uploading",
                stage: "queued",
                current: 0,
            },
        ]),
    ).toMatchObject({
        device: { pending: false, received: 2 },
        uploadedBytes: 40,
        displayBytes: 40,
        progress: 0.2,
    });
});

it("puts pending device batches before Telegram work even when they are older", () => {
    const device = item({ batchId: "device", batchCreatedAt: 1000 });
    const telegram = item({
        key: "telegram",
        batchId: "telegram",
        batchCreatedAt: 9000,
        jobId: "job",
        state: "uploading",
        stage: "uploading",
    });
    expect(
        summarizeBatches([telegram, device], 10000).map((batch) => batch.id),
    ).toEqual(["device", "telegram"]);
});

it("does not count finalizing bytes twice or invent cancelled upload completion", () => {
    const done = item({ state: "completed" });
    const sending = item({
        key: "sending",
        state: "uploading",
        jobId: "job",
        stage: "finalizing",
        current: 100,
    });
    expect(summary([done, sending])).toMatchObject({
        uploadedBytes: 200,
        totalBytes: 200,
        progress: 0.99,
    });
    expect(summary([done, { ...sending, state: "error" }])).toMatchObject({
        uploadedBytes: 100,
        progress: 0.5,
    });
});

it("keeps one folder category until every child completes", () => {
    const running = item({
        key: "running",
        state: "uploading",
        stage: "uploading",
    });
    const queued = item({ key: "queued", state: "queued" });
    const completed = item({ key: "done", state: "completed" });
    expect(batchCategory(summary([running, queued, completed]))).toBe(
        "running",
    );
    expect(batchCategory(summary([queued, completed]))).toBe("queued");
    expect(
        batchCategory(
            summary([queued, item({ key: "failed-sibling", state: "error" })]),
        ),
    ).toBe("queued");
    expect(batchCategory(summary([queued]))).toBe("queued");
    expect(batchCategory(summary([completed]))).toBe("completed");
    expect(
        batchCategory(
            summary([completed, item({ key: "failed", state: "error" })]),
        ),
    ).toBe("error");
    expect(
        batchCategory(
            summary([
                completed,
                item({ key: "cancelled", state: "cancelled" }),
            ]),
        ),
    ).toBe("cancelled");
});
