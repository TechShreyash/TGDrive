import { afterEach, describe, expect, it, vi } from "vitest";
import {
    guardVideoPosterSeek,
    loadVideoPoster,
    videoPosterUrl,
} from "./videoPoster";

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0xff, 0xd9]);
afterEach(() => vi.unstubAllGlobals());

describe("representative poster seek ownership", () => {
    function fixture() {
        const core = {
            resetClockToStartForPoster: vi.fn(),
            seek: vi.fn(async () => {}),
            on: vi.fn(),
            off: vi.fn(),
        };
        const player = Object.assign(new EventTarget(), {
            player: core,
            removeAttribute: vi.fn(),
        });
        const reset = core.resetClockToStartForPoster;
        const detach = guardVideoPosterSeek(
            player as unknown as Parameters<typeof guardVideoPosterSeek>[0],
        );
        return { core, player, reset, detach };
    }

    it("resets an uninterrupted representative frame to zero", () => {
        const { core, reset, detach } = fixture();
        core.resetClockToStartForPoster();
        expect(reset).toHaveBeenCalledOnce();
        detach();
        expect(core.resetClockToStartForPoster).toBe(reset);
    });

    it("does not reset a newer public seek", () => {
        const { core, player, reset, detach } = fixture();
        player.dispatchEvent(new Event("seeking"));
        core.resetClockToStartForPoster();
        expect(reset).not.toHaveBeenCalled();
        expect(player.removeAttribute).toHaveBeenCalledWith("postertime");
        detach();
    });

    it("replaces a pending representative seek with zero on early Play", () => {
        const { core, player, reset, detach } = fixture();
        player.dispatchEvent(new Event("play"));
        expect(core.seek).toHaveBeenCalledExactlyOnceWith(0);
        core.resetClockToStartForPoster();
        expect(reset).not.toHaveBeenCalled();
        player.dispatchEvent(new Event("play"));
        expect(core.seek).toHaveBeenCalledOnce();
        detach();
    });

    it("stops intercepting Play after an ordinary seek completes", () => {
        const { core, player, detach } = fixture();
        const complete = core.on.mock.calls[0][1] as () => void;
        complete();
        player.dispatchEvent(new Event("play"));
        expect(core.seek).not.toHaveBeenCalled();
        detach();
        expect(core.off).toHaveBeenCalledWith("seekcomplete", complete);
    });
});

describe("optional stored video posters", () => {
    it("retains file and share access in the poster URL", () => {
        const url = new URL(
            videoPosterUrl("/folder/file id", "share-token"),
            "https://drive.test",
        );
        expect(url.pathname).toBe("/api/v1/poster");
        expect(url.searchParams.get("path")).toBe("/folder/file id");
        expect(url.searchParams.get("auth")).toBe("share-token");
        expect(videoPosterUrl("/id", "")).toBe("/api/v1/poster?path=%2Fid");
    });

    it("revalidates permission and returns a bounded JPEG for the player", async () => {
        const fetchPoster = vi.fn(
            async () =>
                new Response(jpeg, {
                    headers: { "Content-Type": "image/jpeg; charset=binary" },
                }),
        );
        vi.stubGlobal("fetch", fetchPoster);
        const poster = await loadVideoPoster(
            "/api/v1/poster?path=/id",
            new AbortController().signal,
        );
        expect(poster?.type).toBe("image/jpeg");
        expect(new Uint8Array(await poster!.arrayBuffer())).toEqual(jpeg);
        expect(fetchPoster).toHaveBeenCalledWith(
            "/api/v1/poster?path=/id",
            expect.objectContaining({
                credentials: "same-origin",
                cache: "no-cache",
                headers: { Accept: "image/jpeg" },
            }),
        );
    });

    it.each([204, 401, 403, 404, 503])(
        "falls back for HTTP %s",
        async (status) => {
            vi.stubGlobal(
                "fetch",
                vi.fn(async () => new Response(null, { status })),
            );
            await expect(
                loadVideoPoster("/api/v1/poster", new AbortController().signal),
            ).resolves.toBeNull();
        },
    );

    it.each([
        { mime: "text/html", body: jpeg },
        { mime: "image/jpeg", body: new Uint8Array([1, 2, 3]) },
        { mime: "image/jpeg", body: jpeg.slice(0, -1) },
    ])("rejects an unusable $mime body", async ({ mime, body }) => {
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(body, {
                        headers: { "Content-Type": mime },
                    }),
            ),
        );
        await expect(
            loadVideoPoster("/api/v1/poster", new AbortController().signal),
        ).resolves.toBeNull();
    });

    it.each([true, false])(
        "cancels an oversized image (declared: %s)",
        async (declared) => {
            const cancel = vi.fn();
            vi.stubGlobal(
                "fetch",
                vi.fn(
                    async () =>
                        new Response(
                            new ReadableStream({
                                start(controller) {
                                    if (!declared)
                                        controller.enqueue(
                                            new Uint8Array(1024 * 1024 + 1),
                                        );
                                },
                                cancel,
                            }),
                            {
                                headers: {
                                    "Content-Type": "image/jpeg",
                                    ...(declared
                                        ? {
                                              "Content-Length": String(
                                                  1024 * 1024 + 1,
                                              ),
                                          }
                                        : {}),
                                },
                            },
                        ),
                ),
            );
            await expect(
                loadVideoPoster("/api/v1/poster", new AbortController().signal),
            ).resolves.toBeNull();
            expect(cancel).toHaveBeenCalledOnce();
        },
    );

    it("releases a pending body when the preview closes", async () => {
        const cancel = vi.fn();
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(new ReadableStream({ cancel }), {
                        headers: { "Content-Type": "image/jpeg" },
                    }),
            ),
        );
        const controller = new AbortController();
        const request = loadVideoPoster("/api/v1/poster", controller.signal);
        controller.abort();
        await expect(request).resolves.toBeNull();
        expect(cancel).toHaveBeenCalledOnce();
    });

    it("leaves playback available after a network error", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => {
                throw new TypeError("Offline");
            }),
        );
        await expect(
            loadVideoPoster("/api/v1/poster", new AbortController().signal),
        ).resolves.toBeNull();
    });
});
