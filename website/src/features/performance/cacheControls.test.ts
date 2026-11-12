import { describe, expect, it } from "vitest";
import {
    performanceChanges,
    performanceDraft,
    performanceCategoryDraft,
    MIB,
    type PerformancePreferences,
} from "./performanceApi";
const saved: PerformancePreferences = {
    revision: '"one"',
    timezone: "UTC",
    auto_preload: true,
    upload_split_mib: 2000,
    cache_disk_limit_bytes: null,
    cache_memory_limit_bytes: null,
    staging_limit_bytes: null,
    protected_free_bytes: null,
    upload_limit_bytes_per_second: null,
    download_limit_bytes_per_second: null,
    cache_ttl_seconds: 86400,
    stream_memory_limit_bytes: null,
};
describe("retention and temporary stream ceilings", () => {
    it("uses canonical hours and serializes selected duration to whole seconds", () => {
        const draft = performanceDraft(saved);
        expect(draft.cache_ttl_seconds).toBe("24");
        expect(
            performanceChanges({ ...draft, cache_ttl_seconds: "48" }, saved),
        ).toEqual({ cache_ttl_seconds: 172800 });
        expect(
            performanceChanges(
                { ...draft, cache_ttl_seconds: String(1 / 60) },
                saved,
            ),
        ).toEqual({ cache_ttl_seconds: 60 });
    });
    it.each(["", "0", "-1", "0.001", "Infinity", "garbage", "1e30"])(
        "rejects invalid retention hours %s",
        (value) => {
            expect(() =>
                performanceChanges(
                    { ...performanceDraft(saved), cache_ttl_seconds: value },
                    saved,
                ),
            ).toThrow("Disk cache retention");
        },
    );
    it("preserves exact stored seconds on unrelated saves and isolates cache drafts", () => {
        for (const seconds of [61, 86401, Number.MAX_SAFE_INTEGER]) {
            const baseline = { ...saved, cache_ttl_seconds: seconds };
            expect(
                performanceChanges(
                    { ...performanceDraft(baseline), timezone: "Asia/Kolkata" },
                    baseline,
                ),
            ).toEqual({ timezone: "Asia/Kolkata" });
        }
        const draft = { ...performanceDraft(saved), cache_ttl_seconds: "" };
        expect(
            performanceChanges(
                performanceCategoryDraft(draft, saved, "transfers"),
                saved,
            ),
        ).toEqual({});
    });
    it("keeps stream working memory separate from retained RAM and supports automatic", () => {
        const draft = performanceDraft(saved);
        expect(draft.stream_memory_limit_bytes.mode).toBe("automatic");
        expect(
            performanceChanges(
                {
                    ...draft,
                    stream_memory_limit_bytes: { mode: "custom", value: "2" },
                },
                saved,
            ),
        ).toEqual({ stream_memory_limit_bytes: 2 * MIB });
        const custom = { ...saved, stream_memory_limit_bytes: 16 * MIB };
        expect(
            performanceChanges(
                {
                    ...performanceDraft(custom),
                    stream_memory_limit_bytes: { mode: "automatic", value: "" },
                },
                custom,
            ),
        ).toEqual({ stream_memory_limit_bytes: null });
    });
    it.each(["", "0", "1", "1.999", "Infinity", "1e30"])(
        "rejects invalid working memory MiB %s",
        (value) => {
            expect(() =>
                performanceChanges(
                    {
                        ...performanceDraft(saved),
                        stream_memory_limit_bytes: { mode: "custom", value },
                    },
                    saved,
                ),
            ).toThrow("Stream working memory");
        },
    );
    it("does not expose a disabled stream pool or leak cache edits into transfer saves", () => {
        const draft = {
            ...performanceDraft(saved),
            stream_memory_limit_bytes: { mode: "disabled" as const, value: "" },
        };
        expect(() => performanceChanges(draft, saved)).toThrow("valid mode");
        expect(
            performanceChanges(
                performanceCategoryDraft(draft, saved, "transfers"),
                saved,
            ),
        ).toEqual({});
    });
});
