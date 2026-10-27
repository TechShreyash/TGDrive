import { describe, expect, it, vi } from "vitest";
import { formatByteProgress, formatBytes } from "./types";

describe("byte formatting", () => {
    it("preserves units, precision and invalid values without rebuilding formatters", () => {
        const number = new Intl.NumberFormat(undefined, {
            maximumFractionDigits: 2,
        });
        const constructor = vi.spyOn(Intl, "NumberFormat");
        try {
            expect(formatBytes(0)).toBe("0 B");
            expect(formatBytes(1023)).toBe(number.format(1023) + " B");
            expect(formatBytes(1536)).toBe(number.format(1.5) + " KB");
            expect(formatBytes(1024 ** 5)).toBe(number.format(1024) + " TB");
            expect(formatByteProgress(1536, 2048)).toBe(
                number.format(1.5) + " / 2 KB",
            );
            for (const value of [undefined, -1, NaN, Infinity])
                expect(formatBytes(value)).toBe("—");
            expect(constructor).not.toHaveBeenCalled();
        } finally {
            constructor.mockRestore();
        }
    });
});
