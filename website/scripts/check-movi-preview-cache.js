import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { PREVIEW_CACHE_PATCHES } from "./movi-player-preview-cache.js";

let original = await readFile(
    process.env.TGDRIVE_MOVI_SOURCE ||
        new URL(
            "../node_modules/movi-player/dist/element.slim.js",
            import.meta.url,
        ),
    "utf8",
);
for (const [before, after] of [...PREVIEW_CACHE_PATCHES].reverse())
    original = original.replace(after, before);
let patched = original;
for (const [before, after] of PREVIEW_CACHE_PATCHES) {
    assert.equal(patched.split(before).length, 2, before.slice(0, 100));
    patched = patched.replace(before, after);
}
function classSource(name) {
    const start = patched.indexOf(`class ${name} {`);
    assert.ok(start >= 0, name);
    return patched.slice(start, patched.indexOf("\n}\n", start) + 2);
}
const logger = { debug() {}, info() {}, warn() {}, error() {} };
const Cache = new Function("g", "oA", `return ${classSource("LRUCache")}`)(
    logger,
    "LRUCache",
);
let fetcher = () => assert.fail("unexpected network request");
const fetchProxy = (...args) => fetcher(...args);
let currentClock = Date;
const testDate = { now: () => currentClock.now() };
let thumbnailTimeout = setTimeout;
const thumbnailWindow = Object.assign(new EventTarget(), {
    navigator: { onLine: true },
});
const Http = new Function(
    "g",
    "QA",
    "EA",
    "iA",
    "tA",
    "LRUCache",
    "crossOriginIsolated",
    "fetch",
    "Date",
    `return ${classSource("HttpSource")}`,
)(
    logger,
    "HttpSource",
    2097152,
    262144000,
    131072,
    Cache,
    false,
    fetchProxy,
    testDate,
);
const Thumbnail = new Function(
    "g",
    "rA",
    "hA",
    "self",
    "fetch",
    "setTimeout",
    `return ${classSource("ThumbnailHttpSource")}`,
)(
    logger,
    "ThumbnailHttpSource",
    (signal) => {
        const controller = new AbortController();
        if (signal.aborted) controller.abort();
        else
            signal.addEventListener("abort", () => controller.abort(), {
                once: true,
            });
        return controller;
    },
    thumbnailWindow,
    fetchProxy,
    (...args) => thumbnailTimeout(...args),
);
const previewFrameStart = patched.indexOf("  async getPreviewFrame(");
const previewFrameSource = patched.slice(
    previewFrameStart,
    patched.indexOf("  async generateTimeline(", previewFrameStart),
);
const getPreviewFrame = new Function(
    "g",
    "Je",
    `return ({${previewFrameSource}}).getPreviewFrame`,
)(logger, "MoviPlayer");
const previewReadStart = patched.indexOf(
    "read: async (A3, I3) => {",
    patched.indexOf("  async buildPreviewPipeline("),
);
const previewReadSource = patched.slice(
    previewReadStart,
    patched.indexOf("}, getSize:", previewReadStart) + 1,
);
const createPreviewAdapter = new Function(
    "ThumbnailHttpSource",
    `return {${previewReadSource}}`,
);
function previewOwner(reader) {
    return {
        previewsAllowed: () => true,
        previewKey: () => null,
        trackManager: { getActiveVideoTrack: () => ({}) },
        thumbnailSource: reader,
        thumbnailBindings: { readKeyframe: async () => 0 },
        thumbnailRenderer: { setProjection() {} },
        scheduleThumbBufferClear() {},
        getPreviewFrame,
    };
}
function primary(size = 4096, headers = {}) {
    const source = new Http("https://media.test/file", headers, 8);
    source.size = size;
    return source;
}
function preview(source, headers = source.headers) {
    const reader = new Thumbnail(source.url, headers, source);
    reader.seedSize(source.size);
    return reader;
}
function request(reader) {
    return {
        source: reader.borrowSource,
        preview: reader,
        url: reader.url,
        headers: JSON.stringify([...new Headers(reader.headers)]),
    };
}
function response(data, start, end, size = data.byteLength) {
    return new Response(data.slice(start, end + 1), {
        status: 206,
        headers: { "Content-Range": `bytes ${start}-${end}/${size}` },
    });
}
function bytes(size) {
    return Uint8Array.from({ length: size }, (_, index) => index % 251);
}
function range(options) {
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
    assert.ok(match);
    return match.slice(1).map(Number);
}

test("the reviewed preview substitutions reverse exactly", () => {
    let reversed = patched;
    for (const [before, after] of [...PREVIEW_CACHE_PATCHES].reverse())
        reversed = reversed.replace(after, before);
    assert.equal(reversed, original);
});

test("real thumbnail fetch bytes satisfy a later playback read without another fetch", async () => {
    const data = bytes(4096),
        source = primary(data.length),
        reader = preview(source);
    let calls = 0;
    fetcher = async (_url, options) => {
        calls++;
        const [start, end] = range(options);
        return response(data, start, end);
    };
    assert.deepEqual(
        new Uint8Array(await reader.read(512, 64)),
        data.slice(512, 576),
    );
    assert.equal(calls, 1);
    assert.deepEqual(
        new Uint8Array(await source.read(1000, 100)),
        data.slice(1000, 1100),
    );
    assert.equal(calls, 1);
    assert.equal(
        source.atomicGetWritePos(),
        0,
        "offering bytes must not move playback's window",
    );
    assert.equal(source.peekRange(1000, 100), null);
    assert.equal(
        source.metadataCache.size,
        0,
        "preview data must not displace metadata",
    );
    reader.clearBuffer();
    assert.deepEqual(
        new Uint8Array(await reader.read(1000, 100)),
        data.slice(1000, 1100),
    );
    assert.equal(calls, 1, "future previews borrow the same bounded cache");
    reader.close();
    source.close();
});

test("normal playback range streaming skips preview-owned intervals without holes or double-counting traffic", async () => {
    const data = bytes(1024),
        source = primary(data.length),
        reader = preview(source);
    source.cachePreviewBytes(400, data.slice(400, 600), request(reader));
    const requests = [];
    fetcher = async (_url, options) => {
        const [start, end] = range(options);
        requests.push([start, end]);
        return response(data, start, end);
    };
    source.abortController = new AbortController();
    source.atomicSetStreaming(true);
    await source.readStreamBackground(0);
    assert.deepEqual(requests, [
        [0, 399],
        [600, 1023],
    ]);
    assert.deepEqual(source.peekRange(0, data.length), data);
    assert.equal(source.totalBytesDownloaded, 824);
    assert.equal(source.getBufferedEnd(), data.length);
    reader.close();
    source.close();
});

test("overlapping and adjacent cached fragments stitch only complete byte coverage", () => {
    const data = bytes(256),
        source = primary(data.length),
        reader = preview(source);
    const owner = request(reader);
    source.cachePreviewBytes(80, data.slice(80, 140), owner);
    source.cachePreviewBytes(120, data.slice(120, 180), owner);
    source.cachePreviewBytes(180, data.slice(180, 220), owner);
    assert.deepEqual(source.peekPreviewBytes(100, 110), data.slice(100, 210));
    assert.equal(source.peekPreviewBytes(70, 80), null);
    assert.equal(source.peekPreviewBytes(200, 30), null);
    source.cachePreviewBytes(240, data.slice(240), owner);
    assert.equal(
        source.peekPreviewBytes(210, 40),
        null,
        "a gap must never become zero-filled media",
    );
    assert.deepEqual(
        source.peekPreviewBytes(240, 100),
        data.slice(240),
        "a real EOF tail may be short",
    );
    const copy = source.peekPreviewBytes(100, 20);
    copy.fill(255);
    assert.deepEqual(source.peekPreviewBytes(100, 20), data.slice(100, 120));
    reader.close();
    source.close();
});

test("partly arrived windows expose only bytes actually received", async () => {
    const data = bytes(4096),
        source = primary(data.length),
        reader = preview(source);
    let controller;
    fetcher = async (_url, options) => {
        const [start, end] = range(options);
        return new Response(
            new ReadableStream({
                start(stream) {
                    controller = stream;
                    stream.enqueue(data.slice(start, start + 128));
                },
            }),
            {
                status: 206,
                headers: {
                    "Content-Range": `bytes ${start}-${end}/${data.length}`,
                },
            },
        );
    };
    assert.deepEqual(
        new Uint8Array(await reader.read(512, 64)),
        data.slice(512, 576),
    );
    assert.deepEqual(source.peekPreviewBytes(512, 128), data.slice(512, 640));
    assert.equal(source.peekPreviewBytes(512, 129), null);
    controller.close();
    await Promise.resolve();
    reader.close();
    source.close();
});

test("only matching source, URL, headers, size and Content-Range can share bytes", () => {
    const source = primary(100, { Authorization: "one" }),
        reader = preview(source);
    const owner = request(reader);
    const make = (contentRange, encoding) =>
        new Response(null, {
            status: 206,
            headers: {
                "Content-Range": contentRange,
                ...(encoding ? { "Content-Encoding": encoding } : {}),
            },
        });
    assert.equal(
        reader.previewByteRangeEnd(make("bytes 10-19/100"), 10, 19, owner),
        20,
    );
    for (const header of [
        "bytes 11-19/100",
        "bytes 10-20/100",
        "bytes 10-19/101",
        "bytes 10-19/*",
        "bytes 20-10/100",
        "",
    ])
        assert.equal(
            reader.previewByteRangeEnd(make(header), 10, 19, owner),
            -1,
        );
    assert.equal(
        reader.previewByteRangeEnd(
            make("bytes 10-19/100", "gzip"),
            10,
            19,
            owner,
        ),
        -1,
    );
    reader.borrowSource = primary(100, { Authorization: "one" });
    assert.equal(
        reader.previewByteRangeEnd(make("bytes 10-19/100"), 10, 19, owner),
        -1,
    );
    source.cachePreviewBytes(10, bytes(10), owner);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    reader.borrowSource.close();
    reader.borrowSource = source;
    reader.headers = { Authorization: "two" };
    source.cachePreviewBytes(10, bytes(10), owner);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    reader.headers = source.headers;
    reader.url += "/other";
    source.cachePreviewBytes(10, bytes(10), owner);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    reader.close();
    source.close();
});

test("the byte and entry caps include retained backing arrays", () => {
    const source = primary(100 * 1048576),
        reader = preview(source),
        owner = request(reader);
    const backing = new Uint8Array(16 * 1048576);
    source.cachePreviewBytes(0, backing.subarray(0, 100), owner);
    assert.equal(
        [...source._tgPreviewBytes.cache.values()][0].data.byteLength,
        100,
    );
    source._tgPreviewBytes.clear();
    const chunk = new Uint8Array(1048576);
    for (let i = 0; i < 9; i++)
        source.cachePreviewBytes(i * chunk.length, chunk, owner);
    assert.ok(source._tgPreviewBytes.currentSizeBytes <= 8 * 1048576);
    assert.equal(source.peekPreviewBytes(0, 100), null);
    source._tgPreviewBytes.clear();
    for (let i = 0; i < 129; i++)
        source.cachePreviewBytes(i * 2, new Uint8Array([i]), owner);
    assert.equal(source._tgPreviewBytes.cache.size, 128);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 128);
    reader.close();
    source.close();
});

test("closing either source prevents late offers and playback close clears retained bytes", async () => {
    const source = primary(100),
        reader = preview(source),
        owner = request(reader);
    source.cachePreviewBytes(0, bytes(20), owner);
    reader.close();
    source.cachePreviewBytes(20, bytes(20), owner);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 20);
    source.close();
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    source.cachePreviewBytes(20, bytes(20), owner);
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    await assert.rejects(source.read(0, 20), /Source closed/);
});

test("cached intervals do not alter the server's open-ended range fallback", async () => {
    const source = primary(100),
        reader = preview(source);
    source.cachePreviewBytes(10, bytes(20), request(reader));
    assert.equal(source.previewRangeEnd(0, 50, true), 50);
    let calls = 0;
    fetcher = async (_url, options) => {
        calls++;
        assert.equal(options.headers.Range, "bytes=10-");
        return new Response(null, { status: 206 });
    };
    await source.fetchRangeOrPreview(
        10,
        29,
        new AbortController().signal,
        true,
    );
    assert.equal(calls, 1);
    reader.close();
    source.close();
});

test("cache-only streams leave every network counter and throughput sample unchanged", async () => {
    const data = bytes(1200000),
        source = primary(data.length),
        reader = preview(source);
    source.cachePreviewBytes(0, data, request(reader));
    Object.assign(source, {
        totalBytesDownloaded: 321,
        lastSpeedBytes: 123,
        lastSpeedTime: 5000,
        currentSpeed: 777,
        lastSpeed: 888,
    });
    const fields = [
        "totalBytesDownloaded",
        "lastSpeedBytes",
        "lastSpeedTime",
        "currentSpeed",
        "lastSpeed",
        "streamStartTime",
        "deliveryLog",
        "deliverySamples",
        "bestDeliverySampledAt",
    ];
    const snapshot = () =>
        structuredClone(
            Object.fromEntries(fields.map((key) => [key, source[key]])),
        );
    const before = snapshot();
    fetcher = () => assert.fail("cache-only playback must not fetch");
    source.abortController = new AbortController();
    source.atomicSetStreaming(true);
    let time = 10000;
    currentClock = { now: () => (time += 600) };
    try {
        await source.readStreamBackground(0);
        assert.deepEqual(snapshot(), before);
        assert.deepEqual(source.peekRange(0, data.length), data);
    } finally {
        currentClock = Date;
        reader.close();
        source.close();
    }
});

test("malformed sharing records are ignored without breaking preview extraction", () => {
    const source = primary(100),
        reader = preview(source);
    for (const owner of [
        undefined,
        {},
        { source },
        { source, preview: {} },
        { source, preview: { borrowSource: source } },
    ])
        assert.doesNotThrow(() =>
            source.cachePreviewBytes(0, new Uint8Array([1]), owner),
        );
    assert.equal(
        reader.previewByteRangeEnd(new Response(null, { status: 206 }), 0, 1),
        -1,
    );
    assert.equal(source._tgPreviewBytes.currentSizeBytes, 0);
    reader.close();
    source.close();
});

test("one image shares a 16 MiB request budget across cold metadata reads and later misses", async () => {
    const source = primary(32 * 1048576),
        reader = preview(source),
        owner = previewOwner(reader);
    const adapter = createPreviewAdapter.call(owner, Thumbnail);
    let requested = 0,
        count = 0;
    fetcher = async (_url, options) => {
        const [start, end] = range(options);
        count++;
        requested += end - start + 1;
        return new Response(new Uint8Array(end - start + 1), {
            status: 206,
            headers: {
                "Content-Range": `bytes ${start}-${end}/${source.size}`,
            },
        });
    };
    owner.thumbnailBindings = null;
    let firstBudget;
    owner.initPreviewPipeline = async () => {
        firstBudget = owner._tgPreviewBudget;
        for (let offset = 0; offset < 16 * 1048576; offset += 2 * 1048576)
            assert.equal((await adapter.read(offset, 1)).length, 1);
        owner.thumbnailBindings = {
            readKeyframe: async () => {
                assert.equal((await adapter.read(16 * 1048576, 1)).length, 0);
                return 0;
            },
        };
    };
    assert.equal(await getPreviewFrame.call(owner, 1), null);
    assert.equal(requested, 16 * 1048576);
    assert.equal(count, 8);
    assert.equal(firstBudget.remaining, 0);
    assert.equal(reader._tgPreviewBudget, firstBudget);
    assert.equal(
        (await adapter.read(14 * 1048576, 1)).length,
        1,
        "local window hits are free",
    );
    reader.clearBuffer();
    assert.equal(
        (await adapter.read(12 * 1048576, 1)).length,
        1,
        "shared preview cache hits are free",
    );
    assert.equal(requested, 16 * 1048576);
    owner.thumbnailBindings.readKeyframe = async () => {
        assert.notEqual(owner._tgPreviewBudget, firstBudget);
        assert.equal((await adapter.read(16 * 1048576, 1)).length, 1);
        return 0;
    };
    assert.equal(await getPreviewFrame.call(owner, 2), null);
    assert.equal(
        requested,
        18 * 1048576,
        "the next owned image receives a fresh budget",
    );
    reader.close();
    source.close();
});

test("every retry charges its requested bytes before fetch without exceeding the image budget", async () => {
    const source = primary(32 * 1048576),
        reader = preview(source);
    reader._tgPreviewBudget = { remaining: 16 * 1048576 };
    let requested = 0,
        count = 0;
    fetcher = async (_url, options) => {
        const [start, end] = range(options);
        requested += end - start + 1;
        count++;
        return new Response(null, { status: 503 });
    };
    thumbnailTimeout = (callback, delay) =>
        delay < 10000
            ? (queueMicrotask(callback), 0)
            : setTimeout(callback, delay);
    try {
        assert.equal((await reader.read(0, 5 * 1048576)).byteLength, 0);
        assert.equal(count, 3);
        assert.equal(requested, 15 * 1048576);
        assert.equal(reader._tgPreviewBudget.remaining, 1048576);
    } finally {
        thumbnailTimeout = setTimeout;
        reader.close();
        source.close();
    }
});

test("the unknown-size byte probe is charged while a cache-hit image does not reset the budget", async () => {
    const source = primary(100),
        reader = preview(source),
        owner = previewOwner(reader);
    reader.size = -1;
    reader._tgPreviewBudget = { remaining: 1 };
    const requests = [];
    fetcher = async (_url, options) => {
        requests.push(options);
        return options.method === "HEAD"
            ? new Response(null)
            : new Response(null, {
                  status: 206,
                  headers: { "Content-Range": "bytes 0-0/100" },
              });
    };
    assert.equal(await reader.getSize(), 100);
    assert.equal(reader._tgPreviewBudget.remaining, 0);
    assert.equal(requests.length, 2);
    owner._tgPreviewBudget = reader._tgPreviewBudget;
    owner.previewKey = () => "cached";
    const cached = new Blob(["image"]);
    owner.previewCache = new Map([["cached", cached]]);
    assert.equal(await getPreviewFrame.call(owner, 1), cached);
    assert.equal(owner._tgPreviewBudget.remaining, 0);
    assert.equal(await reader.resolveSizeViaRange(), null);
    assert.equal(
        requests.length,
        2,
        "an exhausted budget must skip even a one-byte range probe",
    );
    reader.close();
    source.close();
});
