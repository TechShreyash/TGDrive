import { describe, expect, it } from "vitest";
import { torrentSelection } from "./TorrentFilePicker";

describe("torrent file choice", () => {
    const files = [{ size: 3 }, { size: 4 }, { size: 5 }];
    it("sends sorted indexes and totals only the chosen files", () => {
        expect(torrentSelection(files, new Set([2, 0]))).toEqual({
            indexes: [0, 2],
            size: 8,
        });
    });
    it("ignores stale indexes and reports an empty choice", () => {
        expect(torrentSelection(files, new Set([7, -1]))).toEqual({
            indexes: [],
            size: 0,
        });
    });
});
