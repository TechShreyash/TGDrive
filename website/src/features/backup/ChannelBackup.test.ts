import { describe, expect, it } from "vitest";
import {
    parseChannelId,
    retryTime,
    validateBackupToken,
} from "./ChannelBackup";

describe("channel identifiers stay exact before a backup destination is sent", () => {
    it("accepts a Telegram channel ID and surrounding copied whitespace", () => {
        expect(parseChannelId(" -1001234567890 ")).toBe(-1001234567890);
    });
    it.each([
        "",
        "0",
        "-0",
        "123456",
        "-1e12",
        "-100.5",
        "-9007199254740992",
        "--100123",
        "-100 123",
    ])("rejects ambiguous, positive or imprecise destination %s", (value) => {
        expect(() => parseChannelId(value)).toThrow(
            "negative numeric channel ID",
        );
    });
});
describe("backup bot credentials", () => {
    it("accepts a complete token copied with surrounding whitespace", () => {
        const token = "123456789:" + "a".repeat(35);
        expect(validateBackupToken(" " + token + " ")).toBe(token);
    });
    it.each([
        "",
        "123456789",
        "123456789:short",
        "123456789:" + "a".repeat(20) + " injected",
    ])(
        "rejects incomplete or internally spaced tokens without echoing them",
        (value) => {
            expect(() => validateBackupToken(value)).toThrow(
                "Enter the full bot token from BotFather.",
            );
        },
    );
});

describe("Telegram retry deadlines", () => {
    it("uses Unix seconds once and accepts persisted ISO deadlines", () => {
        const seconds = Date.parse("2026-10-22T05:00:00Z") / 1000;
        expect(retryTime(seconds)).toContain("2026");
        expect(retryTime(seconds)).toBe(retryTime("2026-10-22T05:00:00Z"));
        expect(retryTime(null)).toBe("");
    });
});
