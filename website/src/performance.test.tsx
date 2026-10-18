import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "./api";
import { CacheCoverage, coverageRanges } from "./Preloads";
import { httpRates, loadPoints, queueWait } from "./PerformancePanel";
import {
  GIB,
  MIB,
  performanceApi,
  performanceChanges,
  performanceDraft,
  type PerformancePreferences,
} from "./performanceApi";

export const preferences: PerformancePreferences = {
  revision: '"revision-one"',
  timezone: "Asia/Kolkata",
  cache_memory_bytes: 256 * MIB,
  cache_disk_bytes: 10 * GIB,
  cache_min_free_bytes: GIB,
  cache_ttl_seconds: 86400,
  auto_preload: true,
  preload_max_jobs: 2,
  telegram_upload_pipeline: 4,
  telegram_request_limit: 8,
  telegram_parts_per_second: 24,
  telegram_media_per_bot: 3,
  telegram_max_queue: 256,
  upload_split_mib: 2000,
};
beforeEach(() => vi.stubGlobal("window", new EventTarget()));
afterEach(() => vi.unstubAllGlobals());

describe("performance settings and HTTP boundaries", () => {
  it("roundtrips saved byte limits and sends only changed unit conversions", () => {
    const draft = performanceDraft(preferences);
    expect(draft.cache_memory_bytes).toBe("256");
    expect(draft.cache_disk_bytes).toBe("10");
    expect(draft.cache_ttl_seconds).toBe("24");
    expect(performanceChanges(draft, preferences)).toEqual({});
    expect(
      performanceChanges(
        {
          ...draft,
          cache_disk_bytes: "1.5",
          cache_memory_bytes: "512",
          cache_ttl_seconds: "2",
        },
        preferences,
      ),
    ).toEqual({
      cache_disk_bytes: 1.5 * GIB,
      cache_memory_bytes: 512 * MIB,
      cache_ttl_seconds: 7200,
    });
    const odd = { ...preferences, cache_disk_bytes: GIB + 31 };
    expect(performanceChanges(performanceDraft(odd), odd)).toEqual({});
  });
  it.each(["", "-1", "NaN", "Infinity", "9000"])(
    "rejects invalid cache memory %s",
    (value) => {
      expect(() =>
        performanceChanges(
          { ...performanceDraft(preferences), cache_memory_bytes: value },
          preferences,
        ),
      ).toThrow("Memory cache");
    },
  );
  it("rejects fractional concurrency and unknown timezones without coercion", () => {
    expect(() =>
      performanceChanges(
        { ...performanceDraft(preferences), preload_max_jobs: "1.5" },
        preferences,
      ),
    ).toThrow("whole number");
    expect(() =>
      performanceChanges(
        { ...performanceDraft(preferences), timezone: "not/a-zone" },
        preferences,
      ),
    ).toThrow("valid timezone");
  });
  it("guards preference writes with the exact revision and same-origin cookies", async () => {
    const fetcher = vi.fn(async () => Response.json(preferences));
    vi.stubGlobal("fetch", fetcher);
    await performanceApi.save({ cache_disk_bytes: GIB }, preferences.revision);
    const [url, options] = fetcher.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/settings/performance");
    expect(options.credentials).toBe("same-origin");
    expect(options.method).toBe("PATCH");
    expect(new Headers(options.headers).get("If-Match")).toBe(
      preferences.revision,
    );
    expect(JSON.parse(options.body as string)).toEqual({
      cache_disk_bytes: GIB,
    });
  });
  it("preserves conflict and preparation-capacity errors for actionable UI", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          { detail: "Reload the changed settings", code: "SETTINGS_CONFLICT" },
          { status: 409 },
        ),
      ),
    );
    await expect(
      performanceApi.save({}, preferences.revision),
    ).rejects.toMatchObject({ status: 409, code: "SETTINGS_CONFLICT" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          { detail: "Increase cache capacity", code: "PRELOAD_CAPACITY" },
          { status: 507 },
        ),
      ),
    );
    await expect(performanceApi.prepare("/private-id")).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(performanceApi.prepare("/private-id")).rejects.toThrow(
      "Increase cache capacity",
    );
  });
  it("encodes cancellation IDs and clears one cache file without passing source locations", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    vi.stubGlobal("fetch", fetcher);
    await performanceApi.cancel("opaque/id");
    await performanceApi.clear("cache-id");
    await performanceApi.clear();
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe("/api/v1/preloads/opaque%2Fid/cancel");
    expect(JSON.parse(calls[1][1].body as string)).toEqual({
      file_id: "cache-id",
    });
    expect(JSON.parse(calls[2][1].body as string)).toEqual({});
  });
});

describe("cache coverage and observed queue estimates", () => {
  it("merges verified coverage, clips EOF and ignores invalid ranges", () => {
    expect(
      coverageRanges(
        [
          [8, 20],
          [-2, 4],
          [3, 8],
          [9, 7],
          [Number.NaN, 8],
        ],
        10,
      ),
    ).toEqual([[0, 10]]);
    expect(coverageRanges([[0, 10]], 0)).toEqual([]);
  });
  it("paints fragmented cached ranges at their byte offsets without claiming complete", () => {
    const html = renderToStaticMarkup(
      <CacheCoverage
        file={{
          id: "x",
          name: "Video",
          size: 100,
          ready_bytes: 99,
          complete: true,
          ready_ranges: [
            [0, 10],
            [90, 100],
          ],
        }}
      />,
    );
    expect(html).toContain('x="900"');
    expect(html).toContain('width="100"');
    expect(html).not.toContain("Complete");
    expect(html).toContain("20 / 100 B cached");
  });
  it("does not turn missing queue samples into a zero wait promise", () => {
    expect(queueWait(null)).toBe("Estimating…");
    expect(queueWait(Number.NaN)).toBe("Estimating…");
    expect(queueWait(0)).toBe("Less than 1s");
    expect(queueWait(61)).toBe("About 2m");
    expect(loadPoints([], "active", 5)).toBe("");
  });
  it("uses measured byte differences and real sample spacing for throughput", () => {
    const sample = (
      at: number,
      download_bytes: number,
      upload_bytes: number,
    ) => ({
      at,
      download_bytes,
      upload_bytes,
      requests: 1,
      active: 0,
      errors: 0,
    });
    expect(
      httpRates([sample(0, 0, 0), sample(2, 200, 40), sample(5, 500, 100)]),
    ).toEqual([
      { at: 2, active: 100, queued: 20 },
      { at: 5, active: 100, queued: 20 },
    ]);
    expect(httpRates([sample(5, 500, 100), sample(6, 0, 0)])).toEqual([]);
    expect(
      loadPoints(
        [
          { at: 0, active: 0, queued: 0 },
          { at: 3, active: 0, queued: 0 },
          { at: 9, active: 0, queued: 0 },
        ],
        "active",
        1,
      ),
    ).toBe("0,120 200,120 600,120");
  });
});
