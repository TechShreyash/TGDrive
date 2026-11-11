import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import FileThumbnail from "./FileThumbnail";
import type { DriveItem } from "../../shared/types";

const item = {
    id: "video-file",
    path: "/",
    name: "film.mkv",
    type: "file",
    size: 1_000_000_000,
} as DriveItem;

describe("saved-position thumbnails", () => {
    it.each([false, true])(
        "shows a raw fraction and accessible time without an image, compact=%s",
        (compact) => {
            const html = renderToStaticMarkup(
                createElement(FileThumbnail, {
                    item,
                    auth: "",
                    compact,
                    progress: {
                        position_seconds: 90,
                        duration_seconds: 120,
                        updated_at: 100,
                    },
                }),
            );
            expect(html).toContain('class="file-playback-progress"');
            expect(html).toContain('style="width:75%"');
            expect(html).toContain(
                'aria-label="Saved playback position for film.mkv"',
            );
            expect(html).toContain('aria-valuetext="1:30 of 2:00"');
            expect(html).toContain('max="120" value="90"');
            expect(html).not.toContain("<img");
            expect(html).not.toContain("watched");
        },
    );

    it("does not derive a duration from file size or apply resume reset rules to the bar", () => {
        expect(
            renderToStaticMarkup(
                createElement(FileThumbnail, { item, auth: "" }),
            ),
        ).not.toContain("file-playback-progress");
        const html = renderToStaticMarkup(
            createElement(FileThumbnail, {
                item,
                auth: "",
                progress: {
                    position_seconds: 119,
                    duration_seconds: 120,
                    updated_at: 100,
                },
            }),
        );
        expect(html).toContain("file-playback-progress");
        expect(html).toContain('value="119"');
    });

    it.each([
        [0, 120],
        [130, 120],
        [1, 0],
        [Number.NaN, 120],
    ])("omits an empty or invalid position %s/%s", (time, duration) => {
        const html = renderToStaticMarkup(
            createElement(FileThumbnail, {
                item,
                auth: "",
                progress: {
                    position_seconds: time,
                    duration_seconds: duration,
                    updated_at: 100,
                },
            }),
        );
        expect(html).not.toContain("file-playback-progress");
        expect(html).not.toContain("<meter");
    });
});
