import { describe, expect, it } from "vitest";
import { videoSourceMemoryBudgetMiB } from "./videoBufferMemory";

describe("compressed video device memory budget", () => {
    it.each([
        [0.25, 32],
        [0.5, 32],
        [1, 64],
        [2, 128],
        [4, 256],
        [8, 512],
        [16, 512],
    ])("uses reported %s GiB to set a %s MiB ceiling", (memory, ceiling) => {
        expect(videoSourceMemoryBudgetMiB(memory)).toBe(ceiling);
    });
    it.each([undefined, 0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
        "uses a bounded fallback for an unavailable or invalid hint: %s",
        (memory) => expect(videoSourceMemoryBudgetMiB(memory)).toBe(128),
    );
});
