import { describe, expect, it } from "vitest";
import { shareExpired } from "./SharesView";

describe("shared link expiry", () => {
    const base = { path: "/file", created_at: "2026-01-01T00:00:00Z" };

    it("treats numeric expiry values as Unix seconds, like date formatting", () => {
        expect(
            shareExpired({ ...base, expires_at: Date.now() / 1000 + 86400 }),
        ).toBe(false);
        expect(
            shareExpired({ ...base, expires_at: Date.now() / 1000 - 86400 }),
        ).toBe(true);
    });

    it("preserves ISO expiry dates and links without an expiry", () => {
        expect(
            shareExpired({
                ...base,
                expires_at: new Date(Date.now() + 86400000).toISOString(),
            }),
        ).toBe(false);
        expect(
            shareExpired({ ...base, expires_at: "2020-01-01T00:00:00Z" }),
        ).toBe(true);
        expect(shareExpired({ ...base, expires_at: null })).toBe(false);
    });
});
