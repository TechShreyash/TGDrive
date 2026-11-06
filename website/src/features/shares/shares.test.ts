import { afterEach, describe, expect, it, vi } from "vitest";
import { shareExpired, shareUrl } from "./SharesView";

afterEach(() => vi.unstubAllGlobals());

it.each([true, false])(
    "opens a %s folder share in its authorized viewer",
    (folder) => {
        vi.stubGlobal("window", {
            location: { origin: "https://drive.example" },
        });
        const url = new URL(
            shareUrl("token+with/symbols=", "nested-id", folder),
        );
        expect(url.pathname).toBe(folder ? "/" : "/embed");
        expect(url.searchParams.get("path")).toBe(
            folder ? "/share_nested-id" : "/nested-id",
        );
        expect(url.searchParams.get("auth")).toBe("token+with/symbols=");
    },
);

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
