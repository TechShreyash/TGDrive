import { describe, expect, it } from "vitest";
import {
    performanceChanges,
    performanceDraft,
    performanceCategoryDraft,
} from "./performanceApi";
import type { PerformancePreferences } from "./performanceApi";
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
    download_limit_bytes_per_second: 1_234_567,
};
describe("aggregate file speed preferences", () => {
    it("converts decimal Mbps to whole payload bytes and keeps directions separate", () => {
        const draft = performanceDraft(saved);
        expect(draft.download_limit_bytes_per_second.value).toBe("9.876536");
        expect(
            performanceChanges(
                {
                    ...draft,
                    upload_limit_bytes_per_second: {
                        mode: "custom",
                        value: "80",
                    },
                },
                saved,
            ),
        ).toEqual({ upload_limit_bytes_per_second: 10_000_000 });
        expect(
            performanceChanges(
                {
                    ...draft,
                    download_limit_bytes_per_second: {
                        mode: "unlimited",
                        value: "",
                    },
                },
                saved,
            ),
        ).toEqual({ download_limit_bytes_per_second: null });
    });
    it.each(["", "0", "-1", "Infinity", "garbage", "0.008", "1e30"])(
        "rejects invalid custom Mbps %s",
        (value) => {
            expect(() =>
                performanceChanges(
                    {
                        ...performanceDraft(saved),
                        upload_limit_bytes_per_second: {
                            mode: "custom",
                            value,
                        },
                    },
                    saved,
                ),
            ).toThrow("Upload from server");
        },
    );
    it("accepts the minimum and rounds selected Mbps once", () => {
        expect(
            performanceChanges(
                {
                    ...performanceDraft(saved),
                    upload_limit_bytes_per_second: {
                        mode: "custom",
                        value: "0.008192",
                    },
                },
                saved,
            ),
        ).toEqual({ upload_limit_bytes_per_second: 1024 });
        expect(
            performanceChanges(
                {
                    ...performanceDraft(saved),
                    upload_limit_bytes_per_second: {
                        mode: "custom",
                        value: "1.000004",
                    },
                },
                saved,
            ),
        ).toEqual({ upload_limit_bytes_per_second: 125001 });
    });
    it("preserves exact existing bytes on unrelated saves and isolates section drafts", () => {
        const high = {
            ...saved,
            download_limit_bytes_per_second: Number.MAX_SAFE_INTEGER,
        };
        const draft = performanceDraft(high);
        expect(
            performanceChanges({ ...draft, timezone: "Asia/Kolkata" }, high),
        ).toEqual({ timezone: "Asia/Kolkata" });
        const edited = {
            ...draft,
            upload_limit_bytes_per_second: {
                mode: "custom" as const,
                value: "80",
            },
        };
        expect(
            performanceChanges(
                performanceCategoryDraft(edited, high, "cache"),
                high,
            ),
        ).toEqual({});
        expect(
            performanceChanges(
                performanceCategoryDraft(edited, high, "transfers"),
                high,
            ),
        ).toEqual({ upload_limit_bytes_per_second: 10_000_000 });
    });
});
