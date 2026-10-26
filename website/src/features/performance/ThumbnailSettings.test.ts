import { expect, it } from "vitest";
import { rebuildPercent, type ThumbnailRebuild } from "./ThumbnailSettings";

it("reports checked files rather than implying byte completion while finding files", () => {
    const state = {
        status: "running",
        total: 0,
        processed: 0,
    } as ThumbnailRebuild;
    expect(rebuildPercent(state)).toBeUndefined();
    expect(rebuildPercent({ ...state, total: 100, processed: 23 })).toBe(23);
    expect(rebuildPercent({ ...state, status: "completed" })).toBe(100);
    expect(rebuildPercent({ ...state, total: 2, processed: 4 })).toBe(100);
});
