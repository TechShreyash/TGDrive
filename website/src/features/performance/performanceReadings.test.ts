import { describe, expect, it } from "vitest";
import {
    measuredNumber,
    megabits,
    customHistoryBounds,
} from "./performanceReadings";
describe("measured server network readings", () => {
    it("keeps zero distinct from absent and invalid measurements", () => {
        expect(measuredNumber(0)).toBe(0);
        for (const value of [null, undefined, NaN, Infinity, -1, "0"]) {
            expect(measuredNumber(value)).toBeNull();
            expect(megabits(value)).toBe("Unavailable");
        }
        expect(megabits(1_000_000)).toBe("8 Mbps");
        expect(megabits(0)).toBe("0 Mbps");
        expect(megabits(1024)).toBe("<0.01 Mbps");
        expect(megabits(11_702.375)).toBe("0.09 Mbps");
        expect(megabits(1_250)).toBe("0.01 Mbps");
        expect(megabits(1)).toBe("<0.01 Mbps");
        expect(megabits(1_234_567)).toBe("9.88 Mbps");
    });
    it("parses absolute UTC custom bounds and rejects normalized dates, reversed or excessive ranges", () => {
        const now = Date.parse("2026-10-20T12:00:00Z") / 1000;
        expect(
            customHistoryBounds("2026-10-20T10:00", "2026-10-20T11:00", now),
        ).toEqual({ start: now - 7200, end: now - 3600 });
        expect(() =>
            customHistoryBounds("2026-02-30T00:00", "2026-10-20T11:00", now),
        ).toThrow("valid start");
        expect(() =>
            customHistoryBounds("2026-10-20T11:00", "2026-10-20T10:00", now),
        ).toThrow("after start");
        expect(() =>
            customHistoryBounds("2026-09-01T00:00", "2026-10-20T11:00", now),
        ).toThrow("at most 31");
        expect(() =>
            customHistoryBounds("2026-10-20T10:00", "2026-10-21T11:00", now),
        ).toThrow("future");
    });
});
