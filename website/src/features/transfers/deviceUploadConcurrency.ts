export const INITIAL_DEVICE_UPLOAD_CONCURRENCY = 2;
export const MAX_DEVICE_UPLOAD_CONCURRENCY = 4;

const WINDOW_MS = 4000;
const MAX_SAMPLE_GAP_MS = 12000;
const STALL_MS = 15000;
const COOLDOWN_MS = 30000;
interface Chunk {
    bytes: number;
    total?: number;
}
interface Probe {
    previousLimit: number;
    baseline: number;
    rates: number[];
}

/** Measures only device PATCH payloads. All times are supplied by the caller. */
export class DeviceUploadConcurrency {
    private concurrency = INITIAL_DEVICE_UPLOAD_CONCURRENCY;
    private chunks = new Map<string, Chunk>();
    private windowAt?: number;
    private lastAt?: number;
    private bytes = 0;
    private busyTime = 0;
    private baseline?: number;
    private stableWindows = 0;
    private degradedWindows = 0;
    private cooldownUntil = 0;
    private probe?: Probe;
    private lastPayloadProgressAt?: number;
    private paused = false;

    get limit() {
        return this.concurrency;
    }

    chunkStarted(id: string, now: number, totalBytes?: number) {
        this.advance(now);
        if (this.chunks.has(id)) return;
        this.chunks.set(id, {
            bytes: 0,
            total:
                totalBytes !== undefined && Number.isFinite(totalBytes)
                    ? Math.max(0, totalBytes)
                    : undefined,
        });
        // Give a newly started request a full stall interval to make progress.
        if (this.lastPayloadProgressAt !== undefined)
            this.lastPayloadProgressAt = now;
    }

    /** cumulativeBytes is relative to this PATCH body, never a file/resume offset. */
    chunkProgress(id: string, cumulativeBytes: number, now: number) {
        const chunk = this.chunks.get(id);
        if (!chunk || !Number.isFinite(cumulativeBytes) || cumulativeBytes < 0)
            return;
        const measurable = this.advance(now);
        const next = Math.max(
            chunk.bytes,
            Math.min(cumulativeBytes, chunk.total ?? cumulativeBytes),
        );
        if (measurable && next > chunk.bytes) {
            this.bytes += next - chunk.bytes;
            this.lastPayloadProgressAt = now;
        }
        chunk.bytes = next;
    }

    /** A successful receipt supplies a fallback for transports without progress. */
    chunkFinished(id: string, now: number, confirmedBytes?: number) {
        const chunk = this.chunks.get(id);
        if (!chunk) return;
        const measurable = this.advance(now);
        if (confirmedBytes !== undefined && Number.isFinite(confirmedBytes)) {
            const final = Math.min(
                confirmedBytes,
                chunk.total ?? confirmedBytes,
            );
            if (measurable) this.bytes += Math.max(0, final - chunk.bytes);
        }
        this.chunks.delete(id);
        if (!this.sendingCount()) this.lastPayloadProgressAt = undefined;
    }

    /** Call regularly while visible, including during a single long file. */
    sample(now: number, hasBacklog: boolean): number {
        if (this.paused) {
            this.paused = false;
            this.restartWindow(now);
            return this.limit;
        }
        if (!this.advance(now)) return this.limit;
        if (
            this.sendingCount() &&
            this.lastPayloadProgressAt !== undefined &&
            now - this.lastPayloadProgressAt >= STALL_MS
        ) {
            this.reportPressure(now);
            return this.limit;
        }
        const elapsed = now - this.windowAt!;
        if (elapsed < WINDOW_MS) return this.limit;
        const rate = (this.bytes * 1000) / elapsed;
        const saturated = this.busyTime >= elapsed * this.limit * 0.7;
        this.windowAt = now;
        this.bytes = this.busyTime = 0;

        // Preparation, server acknowledgement and a draining queue are not evidence
        // that the network would benefit from another simultaneous payload.
        if (!hasBacklog || !saturated) {
            this.clearTrend();
            return this.limit;
        }
        if (this.probe) {
            this.probe.rates.push(rate);
            if (this.probe.rates.length < 2) return this.limit;
            const average = (this.probe.rates[0] + this.probe.rates[1]) / 2;
            const improved = average >= this.probe.baseline * 1.1;
            this.baseline = improved ? average : this.probe.baseline;
            if (!improved) this.concurrency = this.probe.previousLimit;
            this.probe = undefined;
            this.stableWindows = this.degradedWindows = 0;
            this.cooldownUntil =
                now + (improved ? MAX_SAMPLE_GAP_MS : COOLDOWN_MS);
            return this.limit;
        }
        // A silent transport can be using the receipt fallback. Only actual prior
        // browser progress enables the separate, longer stall timeout above.
        if (!rate) return this.limit;
        if (this.baseline && rate < this.baseline * 0.65) {
            if (++this.degradedWindows >= 2) this.reportPressure(now);
            return this.limit;
        }
        this.degradedWindows = 0;
        const stable =
            this.baseline !== undefined &&
            Math.abs(rate - this.baseline) <= this.baseline * 0.25;
        this.stableWindows = stable ? this.stableWindows + 1 : 1;
        this.baseline = stable ? this.baseline! * 0.75 + rate * 0.25 : rate;
        if (
            rate > 0 &&
            this.stableWindows >= 2 &&
            now >= this.cooldownUntil &&
            this.limit < MAX_DEVICE_UPLOAD_CONCURRENCY
        ) {
            this.probe = {
                previousLimit: this.limit,
                baseline: this.baseline,
                rates: [],
            };
            this.concurrency++;
            this.stableWindows = 0;
        }
        return this.limit;
    }

    /** Only transient device-transport/network/overload failures belong here. */
    reportPressure(now: number) {
        const reduced = Math.max(1, this.limit - 1);
        this.clearTrend();
        this.concurrency = reduced;
        this.cooldownUntil = now + COOLDOWN_MS;
        this.restartWindow(now);
    }

    /** Hidden tabs discard measurement windows while preserving request offsets. */
    pauseMeasurements(now: number) {
        this.clearTrend();
        this.restartWindow(now);
        this.paused = true;
    }

    /** Use only at true idle, after both device work and its queue have drained. */
    reset(now: number) {
        this.chunks.clear();
        this.clearTrend();
        this.concurrency = INITIAL_DEVICE_UPLOAD_CONCURRENCY;
        this.cooldownUntil = 0;
        this.paused = false;
        this.restartWindow(now);
    }

    private sendingCount() {
        let count = 0;
        for (const chunk of this.chunks.values())
            if (chunk.total === undefined || chunk.bytes < chunk.total) count++;
        return count;
    }

    private clearTrend() {
        if (this.probe) this.concurrency = this.probe.previousLimit;
        this.probe = undefined;
        this.baseline = undefined;
        this.stableWindows = this.degradedWindows = 0;
    }

    private restartWindow(now: number) {
        this.windowAt = this.lastAt = now;
        this.bytes = this.busyTime = 0;
        this.lastPayloadProgressAt = undefined;
    }

    private advance(now: number): boolean {
        if (this.paused || !Number.isFinite(now)) return false;
        if (
            this.lastAt === undefined ||
            now < this.lastAt ||
            now - this.lastAt > MAX_SAMPLE_GAP_MS ||
            now - this.windowAt! > MAX_SAMPLE_GAP_MS
        ) {
            this.clearTrend();
            this.restartWindow(now);
            return false;
        }
        this.busyTime +=
            (now - this.lastAt) * Math.min(this.sendingCount(), this.limit);
        this.lastAt = now;
        return true;
    }
}
