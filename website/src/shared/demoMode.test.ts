import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

beforeEach(() => vi.stubGlobal("window", new EventTarget()));
afterEach(() => vi.unstubAllGlobals());

describe("public demo permissions and protected content", () => {
    it("requests fresh session-specific configuration after a login change", async () => {
        const fetcher = vi.fn(async () =>
            Response.json({
                demo_mode: true,
                demo_read_only: false,
                demo_admin: true,
            }),
        );
        vi.stubGlobal("fetch", fetcher);
        await expect(api.config()).resolves.toMatchObject({
            demo_read_only: false,
            demo_admin: true,
        });
        expect(fetcher).toHaveBeenCalledWith(
            "/api/config",
            expect.objectContaining({
                cache: "no-store",
                credentials: "same-origin",
            }),
        );
    });

    it("announces a protected mutation for the root popup while retaining the API error", async () => {
        const denied = vi.fn();
        window.addEventListener("tgdrive:demo-files-read-only", denied);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json(
                    {
                        code: "DEMO_FILES_READ_ONLY",
                        detail: "Demo Files is read-only.",
                    },
                    { status: 403 },
                ),
            ),
        );
        await expect(
            api.folder("/tgdrive-demo-files", "New folder"),
        ).rejects.toMatchObject({
            status: 403,
            code: "DEMO_FILES_READ_ONLY",
        });
        expect(denied).toHaveBeenCalledOnce();
    });

    it("keeps normal file operations and unrelated failures out of the protection popup", async () => {
        const denied = vi.fn();
        window.addEventListener("tgdrive:demo-files-read-only", denied);
        vi.stubGlobal(
            "fetch",
            vi
                .fn()
                .mockResolvedValueOnce(Response.json({ status: "ok" }))
                .mockResolvedValueOnce(
                    Response.json(
                        {
                            code: "NAME_CONFLICT",
                            detail: "Choose another name.",
                        },
                        { status: 409 },
                    ),
                ),
        );
        await api.folder("/", "New folder");
        await expect(api.folder("/", "New folder")).rejects.toMatchObject({
            code: "NAME_CONFLICT",
        });
        expect(denied).not.toHaveBeenCalled();
    });
});
