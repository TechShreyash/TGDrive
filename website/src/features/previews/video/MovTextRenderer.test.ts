import { describe, expect, it } from "vitest";
import {
    decodeMovText,
    isMovTextCodec,
    MOVTEXT_LIMITS,
    MovTextCueBuffer,
} from "./MovTextRenderer";

function sample(text: string) {
    const bytes = new TextEncoder().encode(text);
    return new Uint8Array([bytes.length >> 8, bytes.length & 255, ...bytes]);
}
function packet(text: string, timestamp = 0, duration = 2, streamIndex = 3) {
    return { data: sample(text), timestamp, duration, streamIndex };
}
describe("MP4 timed text decoding", () => {
    it("recognizes only the explicitly handled codecs", () => {
        expect(isMovTextCodec("mov_text")).toBe(true);
        expect(isMovTextCodec("tx3g")).toBe(true);
        for (const codec of ["srt", "ass", "unknown", undefined])
            expect(isMovTextCodec(codec)).toBe(false);
    });
    it("preserves Unicode and inert markup while ignoring trailing style boxes", () => {
        const source = sample(
            "資料 résumé <img src=x onerror=alert(1)>\r\nsecond",
        );
        const bytes = new Uint8Array([
            ...source,
            0,
            0,
            0,
            8,
            115,
            116,
            121,
            108,
        ]);
        expect(decodeMovText(bytes)).toBe(
            "資料 résumé <img src=x onerror=alert(1)>\nsecond",
        );
    });
    it("decodes both BOM-marked UTF-16 byte orders", () => {
        expect(
            decodeMovText(
                new Uint8Array([0, 6, 0xfe, 0xff, 0, 65, 0x4e, 0x2d]),
            ),
        ).toBe("A中");
        expect(
            decodeMovText(
                new Uint8Array([0, 6, 0xff, 0xfe, 65, 0, 0x2d, 0x4e]),
            ),
        ).toBe("A中");
    });
    it("rejects truncated lengths and malformed Unicode rather than guessing text", () => {
        for (const bytes of [
            [],
            [0],
            [0, 2, 65],
            [0, 2],
            [0, 1, 0xff],
            [0, 3, 0xfe, 0xff, 0],
            [0, 4, 0xfe, 0xff, 0xd8, 0],
        ])
            expect(decodeMovText(new Uint8Array(bytes))).toBeNull();
    });
    it("bounds packet and decoded text while accepting explicit empty markers", () => {
        expect(decodeMovText(new Uint8Array([0, 0]))).toBe("");
        expect(
            decodeMovText(new Uint8Array(MOVTEXT_LIMITS.packetBytes + 1)),
        ).toBeNull();
        expect(
            decodeMovText(sample("x".repeat(MOVTEXT_LIMITS.cueCharacters + 1))),
        ).toBeNull();
    });
});
describe("bounded timed subtitle state", () => {
    it("uses packet timing, exclusive cue endings and track identity", () => {
        const buffer = new MovTextCueBuffer();
        buffer.configure(3);
        buffer.pushPacket(packet("wrong", 0, 3, 2));
        buffer.pushPacket(packet("first", 1, 2));
        expect(buffer.textAt(0.9)).toBe("");
        expect(buffer.textAt(1)).toBe("first");
        expect(buffer.textAt(2.99)).toBe("first");
        expect(buffer.textAt(3)).toBe("");
        expect(buffer.queuedCueCount).toBe(1);
    });
    it("drops invalid timing and duplicate packets", () => {
        const buffer = new MovTextCueBuffer();
        buffer.configure(3);
        for (const [start, duration] of [
            [NaN, 2],
            [0, Infinity],
            [Infinity, 2],
            [0, -1],
            [0, 0],
            [1e308, 1e308],
        ])
            buffer.pushPacket(packet("bad", start, duration));
        buffer.pushPacket(packet("valid"));
        buffer.pushPacket(packet("valid"));
        expect(buffer.queuedCueCount).toBe(1);
        expect(buffer.textAt(5)).toBe("");
        buffer.pushPacket(packet("old"));
        expect(buffer.queuedCueCount).toBe(2);
    });
    it("preserves recent ended cues for positive/negative delay adjustments", () => {
        const buffer = new MovTextCueBuffer();
        buffer.configure(3);
        buffer.pushPacket(packet("delayed", 1, 2));
        expect(buffer.textAt(3.05)).toBe("");
        buffer.setDelay(0.1);
        expect(buffer.textAt(3.05)).toBe("delayed");
        buffer.setDelay(-0.1);
        expect(buffer.textAt(3.05)).toBe("");
        buffer.setDelay(2);
        expect(buffer.textAt(3.1)).toBe("delayed");
    });
    it("clears queued and displayed state on seek/track replacement", () => {
        const buffer = new MovTextCueBuffer();
        buffer.configure(3);
        buffer.pushPacket(packet("before"));
        expect(buffer.textAt(0)).toBe("before");
        buffer.clear();
        expect(buffer.textAt(0)).toBe("");
        buffer.pushPacket(packet("after", 8));
        expect(buffer.textAt(8)).toBe("after");
        buffer.configure(null);
        buffer.pushPacket(packet("off"));
        expect(buffer.textAt(0)).toBe("");
    });
    it("bounds count, character budget and simultaneous visible text", () => {
        const buffer = new MovTextCueBuffer();
        buffer.configure(3);
        for (let index = 0; index < MOVTEXT_LIMITS.queuedCues + 32; index++)
            buffer.pushPacket(packet(String(index), index, 2000));
        expect(buffer.queuedCueCount).toBe(MOVTEXT_LIMITS.queuedCues);
        expect(buffer.textAt(999).split("\n")).toHaveLength(
            MOVTEXT_LIMITS.visibleCues,
        );
        buffer.clear();
        for (let index = 0; index < 400; index++)
            buffer.pushPacket(packet("x".repeat(8000), index, 2000));
        expect(buffer.queuedCharacters).toBeLessThanOrEqual(
            MOVTEXT_LIMITS.queuedCharacters,
        );
        expect(buffer.textAt(99).length).toBeLessThanOrEqual(
            MOVTEXT_LIMITS.cueCharacters,
        );
    });
});
