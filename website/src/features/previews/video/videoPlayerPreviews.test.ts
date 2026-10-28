import { describe, expect, it } from "vitest";
import { chapterPreviewInView } from "./videoPlayerPreviews";

describe("chapter preview work window", () => {
    const strip = { left: 100, right: 500, width: 400 };

    it("includes visible cards and only one neighbouring card on either side", () => {
        for (const left of [-59, 0, 100, 300, 499, 659])
            expect(
                chapterPreviewInView(
                    { left, right: left + 160, width: 160 },
                    strip,
                ),
            ).toBe(true);
        for (const left of [-220, -221, 660, 820])
            expect(
                chapterPreviewInView(
                    { left, right: left + 160, width: 160 },
                    strip,
                ),
            ).toBe(false);
    });

    it("does no work for a hidden or clipped-away strip or a zero-size card", () => {
        expect(
            chapterPreviewInView(
                { left: 100, right: 260, width: 160 },
                { ...strip, width: 0 },
            ),
        ).toBe(false);
        expect(
            chapterPreviewInView(
                { left: 100, right: 260, width: 160 },
                { ...strip, width: -20 },
            ),
        ).toBe(false);
        expect(
            chapterPreviewInView({ left: 100, right: 100, width: 0 }, strip),
        ).toBe(false);
    });
});
