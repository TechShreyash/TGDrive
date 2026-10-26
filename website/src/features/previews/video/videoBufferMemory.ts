/** A coarse device hint, not a measurement of currently free browser memory. */
export function videoSourceMemoryBudgetMiB(deviceMemoryGiB?: number): number {
    if (
        deviceMemoryGiB === undefined ||
        !Number.isFinite(deviceMemoryGiB) ||
        deviceMemoryGiB <= 0
    )
        return 128;
    // About 1/16 of reported RAM, bounded to 32–512 MiB. Decoders, GPU
    // surfaces, WASM, transport and the rest of the application need room too.
    return Math.min(512, Math.max(32, Math.floor(deviceMemoryGiB * 64)));
}
