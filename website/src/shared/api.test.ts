import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request } from "./api";

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("request transport failures", () => {
    it.each(["headers", "body"])(
        "still times out stalled %s when a caller supplies a lifecycle signal",
        async (stage) => {
            const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
            const deadline = vi
                .spyOn(AbortSignal, "timeout")
                .mockImplementation((milliseconds) => {
                    expect(milliseconds).toBe(60000);
                    return nativeTimeout(10);
                });
            const controller = new AbortController();
            const fetcher = vi.fn((_url: string, options: RequestInit) => {
                const signal = options.signal!;
                const stalled = new Promise<never>((_resolve, reject) => {
                    signal.addEventListener(
                        "abort",
                        () => reject(signal.reason),
                        { once: true },
                    );
                });
                if (stage === "headers") return stalled;
                const response = new Response();
                vi.spyOn(response, "text").mockReturnValue(stalled);
                return Promise.resolve(response);
            });
            vi.stubGlobal("fetch", fetcher);

            await expect(
                api.directory("/", "", controller.signal),
            ).rejects.toMatchObject({ status: 408, code: "REQUEST_TIMEOUT" });
            expect(deadline).toHaveBeenCalledOnce();
            expect(controller.signal.aborted).toBe(false);
            expect(fetcher).toHaveBeenCalledOnce();
        },
    );

    it("keeps explicit caller cancellation distinct from the request deadline", async () => {
        const controller = new AbortController();
        const cancelled = new DOMException("Leaving this page", "AbortError");
        vi.stubGlobal(
            "fetch",
            vi.fn(
                (_url: string, options: RequestInit) =>
                    new Promise<Response>((_resolve, reject) => {
                        options.signal!.addEventListener(
                            "abort",
                            () => reject(options.signal!.reason),
                            { once: true },
                        );
                    }),
            ),
        );
        const pending = request("/api/v1/settings", undefined, {
            signal: controller.signal,
        });
        controller.abort(cancelled);
        await expect(pending).rejects.toBe(cancelled);
    });

    it("preserves the longer deadline for connection saves and Premium verification", async () => {
        const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
        const deadline = vi.spyOn(AbortSignal, "timeout").mockImplementation(
            // Scale time without replacing the native abort signal or cancellation.
            (milliseconds) => nativeTimeout(milliseconds === 120000 ? 1000 : 5),
        );
        const fetcher = vi.fn(
            (_url: string, options: RequestInit) =>
                new Promise<Response>((resolve, reject) => {
                    expect(options).not.toHaveProperty("timeoutMs");
                    const signal = options.signal!;
                    const aborted = () => {
                        clearTimeout(timer);
                        reject(signal.reason);
                    };
                    const timer = setTimeout(() => {
                        signal.removeEventListener("abort", aborted);
                        resolve(Response.json({ status: "ok" }));
                    }, 25);
                    signal.addEventListener("abort", aborted, { once: true });
                }),
        );
        vi.stubGlobal("fetch", fetcher);
        await expect(
            Promise.all([
                api.saveSettings({ expected_revision: 1 }),
                api.verifyPremiumLogin("challenge", { code: "12345" }),
            ]),
        ).resolves.toEqual([{ status: "ok" }, { status: "ok" }]);
        expect(
            deadline.mock.calls.map(([milliseconds]) => milliseconds),
        ).toEqual([120000, 120000]);
    });

    it.each(["headers", "body"])(
        "reports a timeout while reading %s as a retryable request failure",
        async (stage) => {
            const timeout = new DOMException("Timed out", "TimeoutError");
            const fetcher = vi.fn(async () => {
                if (stage === "headers") throw timeout;
                return {
                    text: async () => {
                        throw timeout;
                    },
                };
            });
            vi.stubGlobal("fetch", fetcher);

            await expect(
                request("/api/v1/uploads/known-receipt"),
            ).rejects.toMatchObject({
                name: "ApiError",
                status: 408,
                code: "REQUEST_TIMEOUT",
            });
            expect(fetcher).toHaveBeenCalledOnce();
        },
    );

    it("normalizes a connection lost after response headers", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => ({
                text: async () => {
                    throw new TypeError("Connection lost");
                },
            })),
        );

        await expect(
            request("/api/v1/uploads/known-receipt"),
        ).rejects.toMatchObject({
            name: "ApiError",
            status: 0,
        });
    });

    it.each(["headers", "body"])(
        "preserves explicit cancellation while reading %s",
        async (stage) => {
            const cancelled = new DOMException("Cancelled", "AbortError");
            vi.stubGlobal(
                "fetch",
                vi.fn(async () => {
                    if (stage === "headers") throw cancelled;
                    return {
                        text: async () => {
                            throw cancelled;
                        },
                    };
                }),
            );

            await expect(request("/api/v1/uploads/known-receipt")).rejects.toBe(
                cancelled,
            );
        },
    );

    it("keeps an HTTP failure status when a proxy sends unreadable JSON", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("Bad gateway", { status: 502 })),
        );

        await expect(
            request("/api/v1/uploads/known-receipt"),
        ).rejects.toMatchObject({
            name: "ApiError",
            status: 502,
        });
    });

    it("does not replay receipt creation when its response is lost", async () => {
        const fetcher = vi.fn(async () => {
            throw new TypeError("Connection lost");
        });
        vi.stubGlobal("fetch", fetcher);

        await expect(
            api.createUpload("file.txt", "/", 4),
        ).rejects.toBeInstanceOf(ApiError);
        expect(fetcher).toHaveBeenCalledOnce();
    });
});

describe("transfer task API", () => {
    it("sends a bounded task page and unfinished folder exclusions", async () => {
        const fetcher = vi.fn(async () => Response.json({ jobs: [] }));
        vi.stubGlobal("fetch", fetcher);
        await api.transferTaskHistory("finished", 3, undefined, "photos", {
            exclude_folders: ["folder-selection-id"],
            offset: 39,
        });
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(url).toBe("/api/v1/transfers/history/tasks");
        expect(options.method).toBe("POST");
        expect(JSON.parse(options.body as string)).toEqual({
            status: "finished",
            page: 3,
            limit: 20,
            q: "photos",
            exclude_folders: ["folder-selection-id"],
            offset: 39,
        });
    });

    it("marks only explicit folder receipts with durable group metadata", async () => {
        const fetcher = vi.fn(async () => Response.json({ id: "receipt" }));
        vi.stubGlobal("fetch", fetcher);
        const folder = {
            id: "folder-selection-id",
            name: "Folder",
            root_path: "/root",
            created_at: 1,
        };
        await api.createUpload("one.bin", "/root", 4, folder);
        await api.createUpload("two.bin", "/root", 4);
        const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
        expect(JSON.parse(calls[0][1].body as string).folder_upload).toEqual(
            folder,
        );
        expect(JSON.parse(calls[1][1].body as string)).not.toHaveProperty(
            "folder_upload",
        );
    });
});
