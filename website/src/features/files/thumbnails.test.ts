import { afterEach, describe, expect, it, vi } from "vitest";
import {
    fetchThumbnail,
    thumbnailCandidate,
    thumbnailUrl,
    MAX_THUMBNAIL_BYTES,
} from "./thumbnails";
const item = {
    id: "id",
    name: "Photo.JPG",
    type: "file" as const,
    path: "/folder",
    size: 1024,
};
afterEach(() => vi.unstubAllGlobals());
describe("bounded provider thumbnails", () => {
    it("requests only likely thumbnail formats, never the original endpoint", () => {
        expect(thumbnailCandidate(item)).toBe(true);
        expect(thumbnailCandidate({ ...item, name: "Manual.pdf" })).toBe(true);
        expect(thumbnailCandidate({ ...item, name: "Archive.zip" })).toBe(
            false,
        );
        expect(thumbnailCandidate({ ...item, type: "folder" })).toBe(false);
        const url = new URL(
            thumbnailUrl(item, "private-token"),
            "https://drive.test",
        );
        expect(url.pathname).toBe("/api/v1/thumbnail");
        expect(url.searchParams.get("auth")).toBe("private-token");
        expect(url.searchParams.get("path")).toBe("/folder/id");
    });
    it("loads a small data image under the existing CSP with permission revalidation", async () => {
        const request = vi.fn(
            async () =>
                new Response(new Uint8Array([1, 2, 3]), {
                    headers: { "Content-Type": "image/png" },
                }),
        );
        vi.stubGlobal("fetch", request);
        expect(
            await fetchThumbnail(
                "/api/v1/thumbnail?path=/a",
                new AbortController().signal,
            ),
        ).toBe("data:image/png;base64,AQID");
        expect(request).toHaveBeenCalledWith(
            expect.any(String),
            expect.objectContaining({
                credentials: "same-origin",
                cache: "no-cache",
            }),
        );
    });
    it.each([204, 401, 403, 404, 503])(
        "leaves an icon fallback for HTTP %s",
        async (status) => {
            vi.stubGlobal(
                "fetch",
                vi.fn(
                    async () =>
                        new Response(status === 204 ? null : "unavailable", {
                            status,
                        }),
                ),
            );
            expect(
                await fetchThumbnail(
                    "/api/v1/thumbnail",
                    new AbortController().signal,
                ),
            ).toBeNull();
        },
    );
    it("rejects declared/actual oversized images and non-image responses", async () => {
        for (const response of [
            new Response("small", {
                headers: {
                    "content-type": "image/png",
                    "content-length": String(MAX_THUMBNAIL_BYTES + 1),
                },
            }),
            new Response(new Uint8Array(MAX_THUMBNAIL_BYTES + 1), {
                headers: { "content-type": "image/png" },
            }),
            new Response("<svg></svg>", {
                headers: { "content-type": "image/svg+xml" },
            }),
        ]) {
            vi.stubGlobal(
                "fetch",
                vi.fn(async () => response),
            );
            expect(
                await fetchThumbnail(
                    "/api/v1/thumbnail",
                    new AbortController().signal,
                ),
            ).toBeNull();
        }
    });
    it("bounds pending work while a fast scroll outruns the network", async () => {
        const releases: (() => void)[] = [];
        const request = vi.fn(
            () =>
                new Promise<Response>((resolve) =>
                    releases.push(() =>
                        resolve(
                            new Response(new Uint8Array([1]), {
                                headers: { "content-type": "image/png" },
                            }),
                        ),
                    ),
                ),
        );
        vi.stubGlobal("fetch", request);
        const controllers = Array.from(
            { length: 35 },
            () => new AbortController(),
        );
        const pending = controllers.map((controller, i) =>
            fetchThumbnail("/thumb/" + i, controller.signal),
        );
        const results = Promise.allSettled(pending);
        expect(request).toHaveBeenCalledTimes(2);
        controllers.slice(2).forEach((controller) => controller.abort());
        releases.forEach((release) => release());
        const settled = await results;
        expect(settled[34]).toMatchObject({
            status: "rejected",
            reason: { name: "QuotaExceededError" },
        });
        expect(request).toHaveBeenCalledTimes(2);
    });
    it("limits concurrency and cancels queued work before it sends anything", async () => {
        const releases: (() => void)[] = [];
        const request = vi.fn(
            () =>
                new Promise<Response>((resolve) =>
                    releases.push(() =>
                        resolve(
                            new Response(new Uint8Array([1]), {
                                headers: { "content-type": "image/png" },
                            }),
                        ),
                    ),
                ),
        );
        vi.stubGlobal("fetch", request);
        const first = fetchThumbnail("/one", new AbortController().signal);
        const second = fetchThumbnail("/two", new AbortController().signal);
        const controller = new AbortController();
        const third = fetchThumbnail("/three", controller.signal);
        expect(request).toHaveBeenCalledTimes(2);
        controller.abort();
        await expect(third).rejects.toMatchObject({ name: "AbortError" });
        releases.forEach((release) => release());
        await Promise.all([first, second]);
        expect(request).toHaveBeenCalledTimes(2);
    });
});
