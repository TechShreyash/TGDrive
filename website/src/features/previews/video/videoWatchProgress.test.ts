import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    formatVideoTime,
    readVideoPosition,
    saveVideoPosition,
} from "./videoWatchProgress";

const prefix = "tgdrive.video.position.v1:";
let stored: Map<string, string>;

beforeEach(() => {
    stored = new Map();
    vi.stubGlobal("localStorage", {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
        removeItem: (key: string) => stored.delete(key),
    });
});
afterEach(() => vi.unstubAllGlobals());

describe("video position labels", () => {
    it.each<[number, string]>([
        [0, "0:00"],
        [37.9, "0:37"],
        [60, "1:00"],
        [3599.9, "59:59"],
        [3600, "1:00:00"],
        [3657, "1:00:57"],
        [4320, "1:12:00"],
        [4635, "1:17:15"],
        [36000, "10:00:00"],
        [86400, "24:00:00"],
    ])("formats %s seconds as %s", (seconds, label) => {
        expect(formatVideoTime(seconds)).toBe(label);
    });
});

describe("local video watch positions", () => {
    it("keeps files independent and clears a finished or restarted video", () => {
        saveVideoPosition("first-file", 37.5);
        saveVideoPosition("second-file", 9);
        expect(readVideoPosition("first-file")).toBe(37.5);
        expect(readVideoPosition("second-file")).toBe(9);
        expect(readVideoPosition("unwatched-file")).toBeNull();
        expect(stored.get(prefix + "first-file")).toBe("37.5");
        saveVideoPosition("first-file", 0);
        expect(stored.has(prefix + "first-file")).toBe(false);
        expect(readVideoPosition("second-file")).toBe(9);
    });

    it.each(["", "garbage", "{}", "null", "-1", "0", "NaN", "Infinity"])(
        "ignores an invalid stored position: %s",
        (value) => {
            stored.set(prefix + "file", value);
            expect(readVideoPosition("file")).toBeNull();
        },
    );

    it.each([-1, Number.NaN, Number.POSITIVE_INFINITY])(
        "preserves a good bookmark when the engine reports %s",
        (value) => {
            saveVideoPosition("file", 37.5);
            saveVideoPosition("file", value);
            expect(readVideoPosition("file")).toBe(37.5);
        },
    );

    it("allows playback when storage access, writes or removal fail", () => {
        vi.stubGlobal("localStorage", {
            getItem() {
                throw new Error("Storage blocked");
            },
            setItem() {
                throw new Error("Storage full");
            },
            removeItem() {
                throw new Error("Storage blocked");
            },
        });
        expect(readVideoPosition("file")).toBeNull();
        expect(() => saveVideoPosition("file", 37.5)).not.toThrow();
        expect(() => saveVideoPosition("file", 0)).not.toThrow();
    });
});
