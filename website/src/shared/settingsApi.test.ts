import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

beforeEach(() => vi.stubGlobal("window", new EventTarget()));
afterEach(() => vi.unstubAllGlobals());

describe("owner management HTTP boundary", () => {
    it("refreshes connections without sending credentials or changing configuration", async () => {
        const fetcher = vi.fn(async () => Response.json({ clients: [] }));
        vi.stubGlobal("fetch", fetcher);
        const controller = new AbortController();
        await api.refreshConnections(controller.signal);
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(url).toBe("/api/v1/settings/refresh");
        expect(options.method).toBe("POST");
        expect(options.credentials).toBe("same-origin");
        expect(options.body).toBe("{}");
        controller.abort();
        expect(options.signal?.aborted).toBe(true);
    });

    it("sends secret replacements only in the mutation body, with a revision guard", async () => {
        const fetcher = vi.fn(async () =>
            Response.json({
                revision: 4,
                restart_required: true,
                admission_paused: true,
            }),
        );
        vi.stubGlobal("fetch", fetcher);
        await api.saveSettings({
            expected_revision: 3,
            api_hash: "a".repeat(32),
            clients: [
                {
                    label: "Storage",
                    kind: "bot",
                    enabled: true,
                    credential: "private-token",
                },
            ],
        });
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(url).toBe("/api/v1/settings");
        expect(url).not.toContain("private-token");
        expect(options.method).toBe("PATCH");
        expect(options.credentials).toBe("same-origin");
        expect(JSON.parse(options.body as string).expected_revision).toBe(3);
    });
    it("uses owner-only phone/code/password bodies and an encoded challenge cancellation path", async () => {
        const fetcher = vi.fn(async () =>
            Response.json({
                stage: "code",
                challenge_id: "challenge",
                expires_in_seconds: 300,
            }),
        );
        vi.stubGlobal("fetch", fetcher);
        await api.startPremiumLogin(4, "+441234567890", "My account");
        await api.verifyPremiumLogin("challenge", { code: "12345" });
        await api.verifyPremiumLogin("challenge", { password: "private-2fa" });
        fetcher.mockImplementationOnce(
            async () => Response.json({ status: "cancelled" }) as never,
        );
        await expect(api.cancelPremiumLogin("opaque/id")).resolves.toEqual({
            status: "cancelled",
        });
        const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
        expect(calls.map(([url]) => url)).toEqual([
            "/api/v1/settings/premium-login/start",
            "/api/v1/settings/premium-login/verify",
            "/api/v1/settings/premium-login/verify",
            "/api/v1/settings/premium-login/opaque%2Fid",
        ]);
        expect(JSON.parse(calls[0][1].body as string)).toEqual({
            expected_revision: 4,
            phone: "+441234567890",
            label: "My account",
        });
        expect(JSON.parse(calls[1][1].body as string)).toEqual({
            challenge_id: "challenge",
            code: "12345",
        });
        expect(JSON.parse(calls[2][1].body as string)).toEqual({
            challenge_id: "challenge",
            password: "private-2fa",
        });
        expect(calls[3][1].method).toBe("DELETE");
    });
    it("retains a server Retry-After so guided login respects an actual provider wait", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    {
                        detail: "Wait before requesting another code.",
                        code: "PREMIUM_LOGIN_WAIT",
                    },
                    { status: 429, headers: { "Retry-After": "17" } },
                ),
            ),
        );
        try {
            await api.startPremiumLogin(1, "+441234567890", "Account");
        } catch (error) {
            expect(error).toBeInstanceOf(ApiError);
            expect((error as ApiError).retryAfter).toBe(17);
            expect((error as ApiError).status).toBe(429);
            return;
        }
        throw new Error("Expected the rate-limited response to reject");
    });
    it("reconciles the global paused state after any authoritative restart-required refusal", async () => {
        const changed = vi.fn();
        window.addEventListener("tgdrive:restart-required", changed);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    {
                        detail: "Restart TGDrive to apply your connection changes.",
                        code: "SETTINGS_RESTART_REQUIRED",
                    },
                    { status: 409 },
                ),
            ),
        );
        await expect(api.folder("/", "new")).rejects.toMatchObject({
            code: "SETTINGS_RESTART_REQUIRED",
        });
        expect(changed).toHaveBeenCalledOnce();
    });
});
