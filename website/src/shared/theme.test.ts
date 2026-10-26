import { describe, expect, it } from "vitest";
import { readThemePreference, resolveTheme, THEME_STORAGE_KEY } from "./theme";
describe("appearance preferences", () => {
    it("respects explicit choices and follows the device only in system mode", () => {
        expect(resolveTheme("light", true)).toBe("light");
        expect(resolveTheme("dark", false)).toBe("dark");
        expect(resolveTheme("system", true)).toBe("dark");
        expect(resolveTheme("system", false)).toBe("light");
    });
    it("accepts only known saved choices and tolerates blocked storage", () => {
        expect(
            readThemePreference({
                getItem: (key) => (key === THEME_STORAGE_KEY ? "dark" : null),
            }),
        ).toBe("dark");
        expect(readThemePreference({ getItem: () => "light" })).toBe("light");
        expect(readThemePreference({ getItem: () => "unexpected" })).toBe(
            "system",
        );
        expect(
            readThemePreference({
                getItem: () => {
                    throw new Error("blocked");
                },
            }),
        ).toBe("system");
    });
});
