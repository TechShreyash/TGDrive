import { vi } from "vitest";

/** Serializes mocked browser owners and aborts queued lock requests. */
export function stubDeviceLocks() {
    const tails = new Map<string, Promise<void>>();
    const request = (
        name: string,
        options: LockOptions,
        work: (lock: Lock) => unknown,
    ) => {
        const previous = tails.get(name) || Promise.resolve();
        let release!: () => void;
        const held = new Promise<void>((resolve) => {
            release = resolve;
        });
        tails.set(
            name,
            previous.then(() => held),
        );
        return new Promise((resolve, reject) => {
            let aborted = false;
            const abort = () => {
                aborted = true;
                release();
                reject(new DOMException("Upload paused", "AbortError"));
            };
            if (options.signal?.aborted) {
                abort();
                return;
            }
            options.signal?.addEventListener("abort", abort, { once: true });
            void previous.then(async () => {
                options.signal?.removeEventListener("abort", abort);
                if (aborted) return;
                try {
                    resolve(await work({ name, mode: "exclusive" }));
                } catch (error) {
                    reject(error);
                } finally {
                    release();
                }
            });
        });
    };
    vi.stubGlobal("navigator", { locks: { request } });
}
