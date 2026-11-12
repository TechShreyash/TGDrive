import { describe, expect, it } from "vitest";
import {
    chapterPreviewInView,
    installPreviewInitializationBudget,
} from "./videoPlayerPreviews";

describe("preview metadata allowance", () => {
    it("opens a large embedded font without spending the frame allowance", async () => {
        const frameBudget = { remaining: 16 * 1024 * 1024 };
        const core = {
            _tgPreviewBudget: frameBudget,
            async initPreviewPipeline() {
                const fontBytes = 23243112 + 17942992;
                if (this._tgPreviewBudget.remaining < fontBytes)
                    throw new Error("metadata allowance exhausted");
                this._tgPreviewBudget.remaining -= fontBytes;
            },
        };
        await expect(core.initPreviewPipeline()).rejects.toThrow();
        const original = core.initPreviewPipeline;
        const detach = installPreviewInitializationBudget(core);
        await core.initPreviewPipeline();
        expect(core._tgPreviewBudget).toBe(frameBudget);
        expect(frameBudget.remaining).toBe(16 * 1024 * 1024);
        detach();
        expect(core.initPreviewPipeline).toBe(original);
    });

    it("keeps initialization bounded and restores the frame allowance on failure", async () => {
        const frameBudget = { remaining: 16 * 1024 * 1024 };
        const core = {
            _tgPreviewBudget: frameBudget,
            async initPreviewPipeline() {
                expect(this._tgPreviewBudget.remaining).toBe(64 * 1024 * 1024);
                throw new Error(
                    "metadata exceeds the initialization allowance",
                );
            },
        };
        const detach = installPreviewInitializationBudget(core);
        await expect(core.initPreviewPipeline()).rejects.toThrow(
            "metadata exceeds",
        );
        expect(core._tgPreviewBudget).toBe(frameBudget);
        detach();
    });
});

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
