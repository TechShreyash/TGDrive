import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "../../shared/api";
import {
    CacheCoverage,
    CacheCapacity,
    coverageRanges,
    cachedFileComplete,
    currentPreloadJobs,
} from "./Preloads";
import {
    httpRates,
    loadPoints,
    queueWait,
    recentSamples,
    samplesInWindow,
    nearestSampleAt,
    customHistoryBounds,
    historyDuration,
    UsageMeter,
    metricSegments,
    metricCeiling,
} from "./PerformancePanel";
import StreamMemoryFact from "./StreamMemoryFact";
import {
    GIB,
    MIB,
    performanceApi,
    performanceChanges,
    performanceDraft,
    performanceCategoryDraft,
    type PerformancePreferences,
    type PreloadJob,
    type ResourceSample,
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
    remote_download_concurrency: 2,
    telegram_upload_pipeline: 4,
    telegram_request_limit: 8,
    telegram_parts_per_second: 24,
    telegram_media_per_bot: 3,
    telegram_max_queue: 256,
    upload_split_mib: 2000,
    max_file_size: 16 * GIB,
    staging_budget_bytes: 16 * GIB,
    min_free_bytes: GIB,
};
beforeEach(() => vi.stubGlobal("window", new EventTarget()));
afterEach(() => vi.unstubAllGlobals());

describe("performance settings and HTTP boundaries", () => {
    it("saves only the visible category while unrelated invalid drafts remain untouched", () => {
        const draft = {
            ...performanceDraft(preferences),
            timezone: "UTC",
            cache_memory_bytes: "",
        };
        expect(
            performanceChanges(
                performanceCategoryDraft(draft, preferences, "regional"),
                preferences,
            ),
        ).toEqual({ timezone: "UTC" });
        expect(draft.cache_memory_bytes).toBe("");
        expect(() =>
            performanceChanges(
                performanceCategoryDraft(draft, preferences, "cache"),
                preferences,
            ),
        ).toThrow("Memory cache");
        expect(
            performanceChanges(
                performanceCategoryDraft(
                    { ...draft, upload_split_mib: "512" },
                    preferences,
                    "transfers",
                ),
                preferences,
            ),
        ).toEqual({ upload_split_mib: 512 });
    });
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
                    {
                        ...performanceDraft(preferences),
                        cache_memory_bytes: value,
                    },
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
        await performanceApi.save(
            { cache_disk_bytes: GIB },
            preferences.revision,
        );
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
                    {
                        detail: "Reload the changed settings",
                        code: "SETTINGS_CONFLICT",
                    },
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
                    {
                        detail: "Increase cache capacity",
                        code: "PRELOAD_CAPACITY",
                    },
                    { status: 507 },
                ),
            ),
        );
        await expect(
            performanceApi.prepare("/private-id"),
        ).rejects.toBeInstanceOf(ApiError);
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
    it("shows actual tier usage and distinguishes zero capacity from unavailable memory", () => {
        const usage = renderToStaticMarkup(
            <CacheCapacity
                label="Memory cache"
                used={80 * MIB}
                limit={256 * MIB}
            />,
        );
        expect(usage).toContain("80 MB");
        expect(usage).toContain("256 MB");
        expect(usage).toContain("31%");
        expect(usage).toContain('aria-valuetext="80 / 256 MB used (31%)"');
        const disabled = renderToStaticMarkup(
            <CacheCapacity
                label="Disk cache"
                used={0}
                limit={0}
                disabled
                unavailable
            />,
        );
        expect(disabled).toContain("Disabled");
        expect(disabled).not.toContain("<progress");
        const pressure = renderToStaticMarkup(
            <CacheCapacity
                label="Memory cache"
                used={20}
                limit={0}
                unavailable
            />,
        );
        expect(pressure).toContain("Unavailable");
        expect(pressure).not.toContain("Disabled");
        expect(pressure).not.toContain("NaN");
    });
    it("uses the latest per-file job before searching or showing old failures", () => {
        const job = (
            file_id: string,
            state: string,
            name: string,
        ): PreloadJob => ({
            id: name,
            file_id,
            state,
            name,
            mode: "manual",
            bytes_done: 0,
            target_bytes: 100,
            size: 100,
            start: 0,
            end: 100,
        });
        const newest = job("finished", "ready", "Renamed movie.mp4");
        const active = job("active", "preparing", "Streaming movie.mp4");
        const cancelled = job("stopped", "cancelled", "Stopped movie.mp4");
        const jobs = currentPreloadJobs([
            newest,
            active,
            cancelled,
            job("finished", "failed", "Old movie.mp4"),
            job("active", "failed", "Old streaming movie.mp4"),
            job("stopped", "failed", "Old stopped movie.mp4"),
        ]);
        expect(jobs).toEqual([newest, active, cancelled]);
        expect(jobs.filter((value) => value.name.includes("Old"))).toEqual([]);
        expect(jobs.filter((value) => value.state === "failed")).toEqual([]);
    });
    it("filters selected history without fabricating samples or joining missing intervals", () => {
        const samples = Array.from({ length: 31 }, (_, index) => ({
            at: index * 30,
        }));
        expect(samplesInWindow(samples, 60)).toEqual([
            { at: 840 },
            { at: 870 },
            { at: 900 },
        ]);
        expect(samplesInWindow(samples, 900)).toHaveLength(31);
        expect(samplesInWindow([{ at: Number.NaN }], 300)).toEqual([]);
        expect(nearestSampleAt([{ at: 0 }, { at: 60 }, { at: 180 }], 100)).toBe(
            60,
        );
        expect(nearestSampleAt([], 100)).toBeNull();
        expect(historyDuration(225)).toBe("3 min 45 sec");
        expect(historyDuration(3600)).toBe("1 hr");
        expect(historyDuration(90000)).toBe("1 day 1 hr");
    });
    it("parses absolute UTC custom bounds and rejects normalized dates, reversed or excessive ranges", () => {
        const now = Date.parse("2026-10-20T12:00:00Z") / 1000;
        expect(
            customHistoryBounds("2026-10-20T10:00", "2026-10-20T11:00", now),
        ).toEqual({ start: now - 7200, end: now - 3600 });
        expect(() =>
            customHistoryBounds("2026-02-30T00:00", "2026-10-20T11:00", now),
        ).toThrow("valid start");
        expect(() =>
            customHistoryBounds("2026-10-20T11:00", "2026-10-20T10:00", now),
        ).toThrow("after start");
        expect(() =>
            customHistoryBounds("2026-09-01T00:00", "2026-10-20T11:00", now),
        ).toThrow("at most 31");
        expect(() =>
            customHistoryBounds("2026-10-20T10:00", "2026-10-21T11:00", now),
        ).toThrow("future");
    });
    it("plots real timestamp spacing and leaves null readings and source changes as gaps", () => {
        const sample = (
            at: number,
            value: number | null,
            scope: "cgroup" | "host" = "cgroup",
        ): ResourceSample => ({
            at,
            cpu_percent: value,
            cpu_scope: scope,
            memory_bytes: null,
            memory_limit_bytes: null,
            received_bytes_per_second: null,
            sent_bytes_per_second: null,
            network_interface: "eth0",
        });
        expect(
            metricSegments(
                [sample(1, 0), sample(4, 50), sample(10, 100)],
                "cpu_percent",
                100,
                "cpu_scope",
            ),
        ).toEqual(["0,112 200,62 600,12"]);
        expect(
            metricSegments(
                [
                    sample(1, 10),
                    sample(4, null),
                    sample(7, 50),
                    sample(10, 60, "host"),
                ],
                "cpu_percent",
                100,
                "cpu_scope",
            ),
        ).toEqual(["0,102", "400,62", "600,52"]);
        expect(
            metricSegments([sample(1, 10), sample(40, 20)], "cpu_percent", 100),
        ).toHaveLength(2);
        expect(metricCeiling(0)).toBe(0);
        expect(metricCeiling(12 * MIB)).toBe(20 * MIB);
    });
    it("keeps charts within a recent continuous window and rejects polling gaps", () => {
        const history = [{ at: 1 }, { at: 4 }, { at: 400 }, { at: 403 }];
        expect(recentSamples(history)).toEqual([{ at: 400 }, { at: 403 }]);
        expect(recentSamples([{ at: 0 }, { at: 300 }, { at: 301 }])).toEqual([
            { at: 300 },
            { at: 301 },
        ]);
        expect(
            httpRates([
                {
                    at: 0,
                    upload_bytes: 0,
                    download_bytes: 0,
                    requests: 0,
                    active: 0,
                    errors: 0,
                },
                {
                    at: 300,
                    upload_bytes: 100,
                    download_bytes: 100,
                    requests: 1,
                    active: 0,
                    errors: 0,
                },
            ]),
        ).toEqual([]);
    });
    it("does not invent resource utilization when the limit is unavailable", () => {
        const html = renderToStaticMarkup(
            <UsageMeter
                label="RAM cache"
                used={0}
                total={0}
                detail="Disabled"
            />,
        );
        expect(html).toContain("Disabled");
        expect(html).not.toContain("progress");
        expect(
            renderToStaticMarkup(
                <UsageMeter label="Server RAM" used={50} total={100} />,
            ),
        ).toContain('value="50"');
    });
    it("shows sampled CPU as a percentage and preserves an unknown baseline", () => {
        expect(
            renderToStaticMarkup(
                <UsageMeter
                    label="Container CPU"
                    used={25}
                    total={100}
                    percent
                />,
            ),
        ).toContain("25.0%");
        const missing = renderToStaticMarkup(
            <UsageMeter
                label="Container CPU"
                used={NaN}
                total={100}
                percent
                unavailableLabel="Measuring…"
            />,
        );
        expect(missing).toContain("Measuring…");
        expect(missing).not.toContain("0.0%");
        expect(missing).not.toContain("progress");
    });
    it("shows the separate effective stream memory budget and tolerates older snapshots", () => {
        expect(renderToStaticMarkup(<StreamMemoryFact />)).toBe("");
        const html = renderToStaticMarkup(
            <StreamMemoryFact
                scratch={{
                    bytes: MIB,
                    max_bytes: 2 * MIB,
                    configured_max_bytes: 32 * MIB,
                    peak_bytes: 2 * MIB,
                    active_streams: 4,
                    waiting_heads: 2,
                }}
            />,
        );
        expect(html).toContain("1 / 2 MB");
        expect(html).not.toContain("32 MB");
        expect(html).toContain("4 streams · 2 waiting for memory");
        expect(html).toContain("separate from retained cache");
    });
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
        expect(
            cachedFileComplete({
                id: "x",
                name: "Video",
                size: 100,
                ready_bytes: 100,
                complete: true,
                ready_ranges: [
                    [0, 10],
                    [90, 100],
                ],
            }),
        ).toBe(false);
        expect(
            cachedFileComplete({
                id: "x",
                name: "Video",
                size: 100,
                ready_bytes: 0,
                complete: false,
                ready_ranges: [
                    [0, 50],
                    [50, 100],
                ],
            }),
        ).toBe(true);
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
        expect(html).toContain("20% · Partial");
        expect(html).toContain('data-complete="false"');
        const complete = renderToStaticMarkup(
            <CacheCoverage
                file={{
                    id: "ready",
                    name: "Ready",
                    size: 100,
                    ready_bytes: 0,
                    complete: false,
                    ready_ranges: [
                        [0, 50],
                        [50, 100],
                    ],
                }}
            />,
        );
        expect(complete).toContain('data-complete="true"');
        expect(complete).toContain(
            'width="1000" height="8" class="cache-coverage-ready"',
        );
        expect(complete).toContain("100% · Ready");
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
            httpRates([
                sample(0, 0, 0),
                sample(2, 200, 40),
                sample(5, 500, 100),
            ]),
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
