import { describe, expect, it } from "vitest";
import { embedUrl } from "./embedLinks";

describe("embed links", () => {
    it.each([
        ["/", "/share_folder-id"],
        ["/file", "/parent/file-id"],
    ])("preserves share access for %s", (route, path) => {
        const share = new URL(route, "https://drive.example");
        share.search = new URLSearchParams({
            path,
            auth: "token+with/symbols=",
        }).toString();
        const embed = new URL(embedUrl(share.toString()));
        expect(embed.origin).toBe(share.origin);
        expect(embed.pathname).toBe("/embed");
        expect(embed.searchParams.get("path")).toBe(path);
        expect(embed.searchParams.get("auth")).toBe("token+with/symbols=");
    });
});
