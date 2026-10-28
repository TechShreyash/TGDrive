import { sampleProgress, type ProgressSample } from "./progress";
import type { TransferBatch } from "./transferBatches";
import { isArchiveTransfer } from "./transferTasks";

export type BatchProgressMeters = ReadonlyMap<string, ProgressSample>;

interface BatchObservation {
    stage: string;
    total: number;
    members: Map<string, string>;
    sample: ProgressSample;
}

/** Browser observations survive file handoffs, but never become recovery data. */
export class BatchProgressTracker {
    private observations = new Map<string, BatchObservation>();
    private meters = new Map<string, ProgressSample>();

    observe(batches: TransferBatch[], now: number) {
        const observations = new Map<string, BatchObservation>();
        const meters = new Map<string, ProgressSample>();
        for (const batch of batches) {
            if (
                !batch.pending ||
                batch.items.some((item) =>
                    ["remote", "torrent", "delete"].includes(item.kind || "") ||
                    isArchiveTransfer(item),
                )
            )
                continue;
            const stage = batch.device.pending ? "device" : "telegram";
            const total = batch.device.pending
                ? batch.device.total
                : batch.totalBytes;
            if (total === undefined || !Number.isFinite(total) || total <= 0)
                continue;
            const bytes = batch.displayBytes;
            if (!Number.isFinite(bytes) || bytes < 0) continue;
            const members = new Map(
                batch.items.map((item) => [
                    item.key,
                    item.size + ":" + (item.state === "cancelled"),
                ]),
            );
            const previous = this.observations.get(batch.id);
            const unchanged =
                previous?.stage === stage &&
                previous.total === total &&
                previous.members.size === members.size &&
                [...members].every(
                    ([key, value]) => previous.members.get(key) === value,
                );
            const sample = sampleProgress(
                unchanged ? previous.sample : undefined,
                stage,
                bytes,
                now,
            );
            observations.set(batch.id, { stage, total, members, sample });
            meters.set(batch.id, sample);
        }
        this.observations = observations;
        this.meters = meters;
    }

    snapshot = (): BatchProgressMeters => this.meters;

    clear() {
        this.observations.clear();
        this.meters = new Map();
    }
}
