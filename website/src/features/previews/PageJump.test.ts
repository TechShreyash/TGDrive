import { describe, expect, it } from "vitest";
import { parsePage } from "./PageJump";

describe("parsePage", () => {
    it("clamps whole numbers to the document", () => {
        expect(parsePage("7", 12)).toBe(7);
        expect(parsePage(" 0 ", 12)).toBe(1);
        expect(parsePage("999", 12)).toBe(12);
    });
    it("rejects anything that is not a page number", () => {
        for (const text of ["", "abc", "2.5", "-3", "1e3"])
            expect(parsePage(text, 12)).toBeNull();
        expect(parsePage("1", 0)).toBeNull();
    });
});
