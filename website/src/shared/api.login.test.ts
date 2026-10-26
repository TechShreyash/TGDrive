import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, sessionExpiryHandler } from "./api";

function busy(retryAfter = "1") {
    return Response.json(
        { detail: "Login busy; retry shortly", code: "LOGIN_BUSY" },
        { status: 429, headers: { "Retry-After": retryAfter } },
    );
}

describe("owner login retries", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it("waits for a busy password worker, then keeps the original login request", async () => {
        const fetcher = vi
            .fn()
            .mockResolvedValueOnce(busy())
            .mockResolvedValueOnce(Response.json({ status: "ok" }));
        vi.stubGlobal("fetch", fetcher);

        const login = api.login("  shared owner password  ");
        await vi.advanceTimersByTimeAsync(999);
        expect(fetcher).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        await expect(login).resolves.toEqual({ status: "ok" });
        expect(fetcher).toHaveBeenCalledTimes(2);
        for (const [url, options] of fetcher.mock.calls) {
            expect(url).toBe("/api/checkPassword");
            expect(options.credentials).toBe("same-origin");
            expect(JSON.parse(options.body)).toEqual({
                pass: "  shared owner password  ",
            });
        }
    });

    it("stops after three total attempts when the worker stays busy", async () => {
        const fetcher = vi.fn().mockImplementation(async () => busy());
        vi.stubGlobal("fetch", fetcher);
        const result = api
            .login("shared owner password")
            .catch((error) => error);

        await vi.advanceTimersByTimeAsync(2000);

        expect(await result).toMatchObject({ status: 429, code: "LOGIN_BUSY" });
        expect(fetcher).toHaveBeenCalledTimes(3);
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(3);
    });

    it.each([
        ["2", 2000],
        ["300", 2000],
        ["0", 1000],
        ["invalid", 1000],
    ])("bounds Retry-After %s to a short wait", async (retryAfter, delay) => {
        const fetcher = vi
            .fn()
            .mockResolvedValueOnce(busy(String(retryAfter)))
            .mockResolvedValueOnce(Response.json({ status: "ok" }));
        vi.stubGlobal("fetch", fetcher);
        const login = api.login("shared owner password");

        await vi.advanceTimersByTimeAsync(Number(delay) - 1);
        expect(fetcher).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(1);
        await expect(login).resolves.toEqual({ status: "ok" });
        expect(fetcher).toHaveBeenCalledTimes(2);
    });

    it.each([
        [401, "INVALID_PASSWORD"],
        [429, "LOGIN_RATE_LIMITED"],
        [429, undefined],
        [500, "LOGIN_BUSY"],
    ])("does not retry status %s with code %s", async (status, code) => {
        const fetcher = vi
            .fn()
            .mockResolvedValue(
                Response.json({ detail: "Login denied", code }, { status }),
            );
        vi.stubGlobal("fetch", fetcher);

        await expect(api.login("shared owner password")).rejects.toBeInstanceOf(
            ApiError,
        );
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(1);
    });

    it("stops retrying when a busy response is followed by an incorrect password", async () => {
        const fetcher = vi
            .fn()
            .mockResolvedValueOnce(busy())
            .mockResolvedValueOnce(
                Response.json({ detail: "Invalid password" }, { status: 401 }),
            );
        vi.stubGlobal("fetch", fetcher);
        const result = api
            .login("incorrect owner password")
            .catch((error) => error);

        await vi.advanceTimersByTimeAsync(1000);

        expect(await result).toMatchObject({
            status: 401,
            message: "Invalid password",
        });
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(2);
    });

    it("does not resubmit credentials after a network failure", async () => {
        const fetcher = vi
            .fn()
            .mockRejectedValue(new TypeError("network unavailable"));
        vi.stubGlobal("fetch", fetcher);

        await expect(api.login("shared owner password")).rejects.toMatchObject({
            status: 0,
        });
        await vi.advanceTimersByTimeAsync(10000);
        expect(fetcher).toHaveBeenCalledTimes(1);
    });
});

describe("session expiry response ordering", () => {
    afterEach(() => vi.unstubAllGlobals());

    it("ignores an old request's 401 after logout and a successful new login, but expires current requests", async () => {
        const expired = vi.fn();
        const events = new EventTarget();
        events.addEventListener("tgdrive:auth-required", expired);
        vi.stubGlobal("window", events);
        let finish!: (response: Response) => void;
        vi.stubGlobal(
            "fetch",
            vi.fn((url: string) =>
                url === "/api/v1/jobs"
                    ? new Promise<Response>((resolve) => {
                          finish = resolve;
                      })
                    : Promise.resolve(Response.json({ status: "ok" })),
            ),
        );
        const pending = api.jobs().catch((error) => error);
        const oldPreview = sessionExpiryHandler();
        await api.logout();
        await api.login("owner");
        finish(
            Response.json({ detail: "Old session expired" }, { status: 401 }),
        );
        expect(await pending).toMatchObject({ status: 401 });
        oldPreview(401);
        expect(expired).not.toHaveBeenCalled();

        const duplicate = sessionExpiryHandler();
        const current = api.jobs().catch((error) => error);
        finish(
            Response.json(
                { detail: "Current session expired" },
                { status: 401 },
            ),
        );
        expect(await current).toMatchObject({ status: 401 });
        duplicate(401);
        expect(expired).toHaveBeenCalledOnce();
        sessionExpiryHandler()(401);
        expect(expired).toHaveBeenCalledTimes(2);
    });

    it("keeps current expiry guards valid when a login is rejected", async () => {
        const events = new EventTarget();
        const expired = vi.fn();
        events.addEventListener("tgdrive:auth-required", expired);
        vi.stubGlobal("window", events);
        const current = sessionExpiryHandler();
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({ detail: "Invalid password" }, { status: 401 }),
            ),
        );
        await expect(api.login("incorrect")).rejects.toMatchObject({
            status: 401,
        });
        expect(expired).not.toHaveBeenCalled();
        current(401);
        expect(expired).toHaveBeenCalledOnce();
    });

    it("expires a current session even if its 401 response body is unreadable", async () => {
        const events = new EventTarget();
        const expired = vi.fn();
        events.addEventListener("tgdrive:auth-required", expired);
        vi.stubGlobal("window", events);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("Unauthorized", { status: 401 })),
        );
        await expect(api.jobs()).rejects.toMatchObject({ status: 401 });
        expect(expired).toHaveBeenCalledOnce();
    });
});
