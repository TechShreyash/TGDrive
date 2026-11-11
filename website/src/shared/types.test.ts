import { afterEach, describe, expect, it, vi } from "vitest";
import {
    dateTimeInputToISO,
    formatByteProgress,
    formatBytes,
    formatDateTimeInput,
    getTimezone,
    setTimezone,
} from "./types";

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

describe("instance timezone expiry inputs", () => {
    const originalZone = getTimezone();
    afterEach(() => setTimezone(originalZone));

    it.each([
        ["UTC", "2030-01-15T18:00", "2030-01-15T18:00:00.000Z"],
        ["Asia/Kolkata", "2030-01-15T18:00", "2030-01-15T12:30:00.000Z"],
        ["Asia/Kathmandu", "2030-01-15T18:00", "2030-01-15T12:15:00.000Z"],
        ["UTC", "9999-12-31T18:00", "9999-12-31T18:00:00.000Z"],
        ["America/New_York", "2030-03-10T03:30", "2030-03-10T07:30:00.000Z"],
        ["America/New_York", "2030-11-03T02:30", "2030-11-03T07:30:00.000Z"],
    ])("round-trips %s wall time %s", (zone, input, expected) => {
        setTimezone(zone);
        expect(dateTimeInputToISO(input)).toBe(expected);
        expect(formatDateTimeInput(expected)).toBe(input);
        expect(formatDateTimeInput(Date.parse(expected) / 1000)).toBe(input);
    });
    it.each([
        ["America/New_York", "2030-03-10T02:30"],
        ["America/New_York", "2030-11-03T01:30"],
        ["Australia/Lord_Howe", "2030-10-06T02:15"],
        ["Australia/Lord_Howe", "2030-04-07T01:45"],
    ])("rejects a DST gap or fold in %s at %s", (zone, input) => {
        setTimezone(zone);
        expect(() => dateTimeInputToISO(input)).toThrow(/clocks change/);
    });
    it.each([
        "2030-02-30T18:00",
        "2030-13-01T00:00",
        "2030-01-15T24:00",
        "2030-01-15T18:00:37",
        "0000-01-01T00:00",
        "not a date",
        "",
    ])("rejects invalid minute input %s", (input) => {
        expect(() => dateTimeInputToISO(input)).toThrow(/valid expiry/);
    });
    it("formats repeated wall times without changing their stored seconds", () => {
        setTimezone("America/New_York");
        expect(formatDateTimeInput("2030-11-03T05:30:37Z")).toBe(
            "2030-11-03T01:30",
        );
        expect(formatDateTimeInput("2030-11-03T06:30:37Z")).toBe(
            "2030-11-03T01:30",
        );
        expect(formatDateTimeInput(null)).toBe("");
        expect(formatDateTimeInput("invalid")).toBe("");
    });
});
