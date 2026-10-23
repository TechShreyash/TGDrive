import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("request transport failures", () => {
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

    await expect(request("/api/v1/uploads/known-receipt")).rejects.toMatchObject({
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

    await expect(request("/api/v1/uploads/known-receipt")).rejects.toMatchObject({
      name: "ApiError",
      status: 502,
    });
  });

  it("does not replay receipt creation when its response is lost", async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError("Connection lost");
    });
    vi.stubGlobal("fetch", fetcher);

    await expect(api.createUpload("file.txt", "/", 4)).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(fetcher).toHaveBeenCalledOnce();
  });
});

describe("transfer task API", () => {
  it("sends a bounded task page and legacy grouping context in the request body", async () => {
    const fetcher = vi.fn(async () => Response.json({ jobs: [] }));
    vi.stubGlobal("fetch", fetcher);
    await api.transferTaskHistory("finished", 3, undefined, "photos", {
      exclude_folders: ["folder-selection-id"],
      offset: 39,
    });
    const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
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
    const folder = { id: "folder-selection-id", name: "Folder", root_path: "/root", created_at: 1 };
    await api.createUpload("one.bin", "/root", 4, folder);
    await api.createUpload("two.bin", "/root", 4);
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    expect(JSON.parse(calls[0][1].body as string).folder_upload).toEqual(folder);
    expect(JSON.parse(calls[1][1].body as string)).not.toHaveProperty("folder_upload");
  });
});
