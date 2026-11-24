import { describe, expect, it, vi } from "vitest";
import { serializeDemuxOperations } from "./movi-demux-queue.js";

function bindings(readFrame: () => Promise<void>) {
    const instance = {
        contextPtr: 1,
        teardownPending: false,
        open: vi.fn(async () => {}),
        seek: vi.fn(async () => {}),
        readFrame,
        scanDuration: vi.fn(async () => {}),
        decodeSubtitle: vi.fn(async () => {}),
        prefetchSubtitleCues: vi.fn(async () => {}),
    };
    const seek = instance.seek;
    serializeDemuxOperations(instance);
    return { instance, seek };
}

describe("Movi demux ownership", () => {
    it("waits for the packet copy before seeking and leaves another player independent", async () => {
        const gate = Promise.withResolvers<void>();
        const { instance, seek } = bindings(() => gate.promise);
        const other = bindings(async () => {});
        const read = instance.readFrame();
        const waiting = instance.seek();
        await other.instance.seek();
        expect(other.seek).toHaveBeenCalledOnce();
        expect(seek).not.toHaveBeenCalled();
        gate.resolve();
        await Promise.all([read, waiting]);
        expect(seek).toHaveBeenCalledOnce();
    });
    it.each([true, false])(
        "releases a failed read (closed=%s)",
        async (closed) => {
            const gate = Promise.withResolvers<void>();
            const { instance, seek } = bindings(() => gate.promise);
            const read = instance.readFrame();
            const waiting = instance.seek();
            instance.teardownPending = closed;
            gate.reject(new Error("read failed"));
            await expect(read).rejects.toThrow("read failed");
            if (closed) {
                await expect(waiting).rejects.toMatchObject({
                    name: "AbortError",
                });
                expect(seek).not.toHaveBeenCalled();
            } else {
                await waiting;
                expect(seek).toHaveBeenCalledOnce();
            }
        },
    );
});
