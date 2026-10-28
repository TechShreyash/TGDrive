import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
    patchMoviSource,
    VERSION,
    ORIGINAL_SHA256,
    BEFORE,
    AFTER,
    NOTICE,
    PATCHES,
    VIDEO_ONLY_AFTER,
    GATE_AFTER,
    RESIZE_AFTER,
    EOF_SETTLED_AFTER,
    digest,
} from "./patch-movi-player.js";

const installed = await readFile(
    process.env.TGDRIVE_MOVI_SOURCE ||
        new URL(
            "../node_modules/movi-player/dist/element.slim.js",
            import.meta.url,
        ),
    "utf8",
);
let original = installed.startsWith(NOTICE)
    ? installed.slice(NOTICE.length)
    : installed;
for (const [before, after] of [...PATCHES].reverse())
    original = original.replace(after, before);

test("the pinned bundle has exactly the reviewed source identity", () => {
    assert.equal(digest(original), ORIGINAL_SHA256);
    assert.equal(original.split(BEFORE).length, 2);
});
// Exercise the shipped policy, including its hysteresis, without downloading
// megabytes or waiting through a long media fixture in every integrity check.
const SourcePolicy = new Function(
    `const EA=2097152; return class {
    ${GATE_AFTER}
  }`,
)();
function policy(rate, ceilingMiB = 128, duration = 600) {
    const source = new SourcePolicy();
    Object.assign(source, {
        size: rate * duration,
        maxBufferSizeMB: ceilingMiB,
        bufferSize: Math.min(32, ceilingMiB) * 1024 ** 2,
        position: 0,
        bufferEnd: 0,
        getPosition() {
            return this.position;
        },
        atomicGetBufferStart() {
            return this.windowStart || 0;
        },
        atomicIsStreaming() {
            return true;
        },
        resizeBuffer(size) {
            this.bufferSize = Math.min(size, ceilingMiB * 1024 ** 2);
        },
    });
    source.setPlaybackBufferGoal(duration, rate * 8);
    return source;
}
test("compressed read-ahead covers time across bitrates within a memory cap", () => {
    for (const rate of [64_000, 512_000, 1_000_000]) {
        const source = policy(rate);
        assert.equal(source.playbackAheadBytes / rate, 90);
    }
    assert.equal(policy(10_000_000).playbackAheadBytes, 0.8 * 128 * 1024 ** 2);
});
test("higher device budgets accommodate 90 seconds of high bitrate video", () => {
    const rate = 32_000_000 / 8;
    const source = policy(rate, 512);
    assert.equal(source.playbackAheadBytes / rate, 90);
    assert.ok(source.bufferSize <= 512 * 1024 ** 2);
    assert.ok(source.bufferSize >= source.playbackAheadBytes / 0.8);
    assert.ok(policy(64_000, 512).bufferSize < source.bufferSize);
});
test("short clips fit completely without applying headroom to their whole size", () => {
    const source = policy(1_875_000, 512, 24);
    assert.equal(source.playbackAheadBytes, source.size);
    assert.equal(source.bufferSize, source.size);
});
const atomicsStart = original.indexOf("  atomicGetWritePos() {");
const atomicsEnd = original.indexOf(
    "  _prefetchThrottled = false;",
    atomicsStart,
);
const streamingStart = original.indexOf("  atomicIsStreaming() {");
const streamingEnd = original.indexOf(
    "  async resolveSize() {",
    streamingStart,
);
function memorySource(shared, ArrayType = Uint8Array) {
    const Source = new Function(
        "crossOriginIsolated",
        "Uint8Array",
        `const EA=2097152; return class {
      bufferWaiters = new Set();
      ${RESIZE_AFTER}
      ${original.slice(atomicsStart, atomicsEnd)}
      ${original.slice(streamingStart, streamingEnd)}
      getBuffer() { return this.useSharedBuffer ? this.dataView : this.fallbackBuffer; }
    }`,
    )(shared, ArrayType);
    const source = new Source();
    source.maxBufferSizeMB = 16;
    source.resizeBuffer(2 * 1024 ** 2);
    return source;
}
for (const shared of [false, true]) {
    test(`source growth retains bytes, offsets and stream state (shared=${shared})`, () => {
        const source = memorySource(shared);
        const bytes = Uint8Array.from({ length: 300_000 }, (_, i) => i % 251);
        source.getBuffer().set(bytes);
        source.atomicSetBufferStart(1_000_000);
        source.atomicSetWritePos(bytes.length);
        source.atomicSetStreaming(true);
        if (shared) Atomics.store(source.headerView, 5, 7);
        source.resizeBuffer(8 * 1024 ** 2);
        assert.equal(source.bufferSize, 8 * 1024 ** 2);
        assert.deepEqual(source.getBuffer().slice(0, bytes.length), bytes);
        assert.equal(source.atomicGetBufferStart(), 1_000_000);
        assert.equal(source.atomicGetWritePos(), bytes.length);
        assert.equal(source.atomicIsStreaming(), true);
        if (shared) assert.equal(Atomics.load(source.headerView, 5), 7);
        source.resizeBuffer(2 * 1024 ** 2);
        assert.equal(source.bufferSize, 8 * 1024 ** 2);
    });
}
test("failed allocation keeps the working buffer and lowers the growth ceiling", () => {
    let refused = 0;
    function LimitedArray(size) {
        if (size > 4 * 1024 ** 2) {
            refused++;
            throw new RangeError("fixture limit");
        }
        return new Uint8Array(size);
    }
    const source = memorySource(false, LimitedArray);
    const old = source.getBuffer();
    old[0] = 71;
    source.atomicSetWritePos(1);
    source.resizeBuffer(8 * 1024 ** 2);
    source.resizeBuffer(16 * 1024 ** 2);
    assert.equal(source.getBuffer(), old);
    assert.equal(old[0], 71);
    assert.equal(refused, 1);
    assert.equal(source.allocationLimitBytes, old.length);
});
test("network fetches continue with a full decoder queue and refill early", async () => {
    const source = policy(512_000);
    source.decoderQueue = 100;
    source.bufferEnd = 512_000 * 5;
    await source.awaitPrefetchGate();
    assert.equal(source.playbackPrefetchHeld, false);
    // Keep fetching when only a few seconds have been decoded; hold at the goal.
    source.bufferEnd = source.playbackAheadBytes;
    setTimeout(() => {
        source.position = source.bufferEnd * 0.1;
    }, 10);
    setTimeout(() => {
        source.position = source.bufferEnd * 0.3;
    }, 160);
    const started = Date.now();
    await source.awaitPrefetchGate();
    assert.ok(Date.now() - started >= 150);
    assert.equal(source.playbackPrefetchHeld, false);
});
test("source closure releases a held read-ahead gate", async () => {
    const source = policy(512_000);
    source.bufferEnd = source.playbackAheadBytes;
    setTimeout(() => {
        source.closed = true;
    }, 10);
    await source.awaitPrefetchGate();
    assert.equal(source.closed, true);
});
test("paused reads and seek cursor jumps do not enlarge speculative read-ahead", () => {
    const source = policy(100_000);
    for (const position of [500_000, 16 * 1024 ** 2, 59_000_000, 500_000]) {
        source.position = position;
        source.setPlaybackBufferGoal(600, 800_000);
        assert.equal(source.playbackAheadBytes, 9_000_000);
        assert.equal(source.bufferSize, 32 * 1024 ** 2);
    }
});
test("declared container bitrate can increase the budget but invalid hints cannot", () => {
    const source = policy(100_000);
    source.setPlaybackBufferGoal(600, 1_600_000);
    assert.equal(source.playbackAheadBytes, 18_000_000);
    for (const bitRate of [NaN, Infinity, -1]) {
        source.setPlaybackBufferGoal(600, bitRate);
        assert.equal(source.playbackAheadBytes, 9_000_000);
    }
});
test("EOF drains queued hardware frames without recreating the HEVC decoder", async () => {
    const patched = patchMoviSource(original, VERSION);
    const start = patched.indexOf(
        "  async flush(drain = false) {",
        patched.indexOf("class MoviVideoDecoder"),
    );
    const method = patched.slice(
        start,
        patched.indexOf("\n  reset() {", start),
    );
    const decoder = new Function(`return ({${method}})`)();
    let drains = 0;
    Object.assign(decoder, {
        decoder: {
            async flush() {
                drains++;
            },
        },
        _drainGeneration: 0,
        lastConfig: { codec: "hvc1.1.6.L150.b0" },
        pendingChunks: [1, 2],
        softRecreateDecoder() {
            throw Error("EOF must not recreate the decoder");
        },
        setWaitingForKeyframe() {
            throw Error("EOF must not discard pending state");
        },
    });
    await decoder.flush(true);
    assert.equal(drains, 1);
    assert.deepEqual(decoder.pendingChunks, [1, 2]);
});
test("the portable patch is deterministic and idempotent", () => {
    const patched = patchMoviSource(original, VERSION);
    assert.ok(patched.startsWith(NOTICE));
    assert.equal(patched.split(AFTER).length, 2);
    assert.equal(patched.split(VIDEO_ONLY_AFTER).length, 2);
    assert.equal(patchMoviSource(patched, VERSION), patched);
});
test("unknown versions and tampered original/patched bundles fail clearly", () => {
    assert.throws(
        () => patchMoviSource(original, "0.4.2"),
        /Review the upstream fix/,
    );
    assert.throws(
        () => patchMoviSource(original + " ", VERSION),
        /does not match/,
    );
    assert.throws(
        () =>
            patchMoviSource(patchMoviSource(original, VERSION) + " ", VERSION),
        /was modified/,
    );
});

test(
    "distant forward and backward seeks do not count the skipped file gap as buffered",
    { timeout: 1000 },
    async () => {
        for (const offset of [3 * 1024 ** 3, 5 * 1024 ** 3]) {
            const source = policy(8_000_000, 512, 734);
            source.position = 67 * 1024 ** 2;
            source.windowStart = offset;
            source.bufferEnd = offset;
            source.playbackPrefetchHeld = true;
            await source.awaitPrefetchGate();
            assert.equal(source.playbackPrefetchHeld, false);
            source.position = offset + 1_000_000;
            source.windowStart = 0;
            source.bufferEnd = 0;
            await source.awaitPrefetchGate();
            assert.equal(source.playbackPrefetchHeld, false);
        }
    },
);

const patched = patchMoviSource(original, VERSION);

test("picked subtitle URLs are revoked once across disconnect and source reset", () => {
    const disconnectStart = patched.indexOf("  disconnectedCallback() {");
    const disconnectEnd = patched.indexOf(
        "\n  attributeChangedCallback(",
        disconnectStart,
    );
    const disposeStart = patched.indexOf("  dispose() {", disconnectEnd);
    const disposeEnd = patched.indexOf("\n  get theme() {", disposeStart);
    assert.ok(disconnectStart >= 0 && disconnectEnd > disconnectStart);
    assert.ok(disposeStart >= 0 && disposeEnd > disposeStart);

    for (const urls of [[], ["blob:first-subtitle", "blob:second-subtitle"]]) {
        for (const steps of [
            ["disconnectedCallback", "dispose", "disconnectedCallback"],
            ["dispose", "disconnectedCallback", "dispose"],
        ]) {
            const revoked = [];
            const tornDown = [];
            const Lifecycle = new Function(
                "URL",
                "document",
                "window",
                "navigator",
                `return class {
                    ${patched.slice(disconnectStart, disconnectEnd)}
                    ${patched.slice(disposeStart, disposeEnd)}
                };`,
            )(
                { revokeObjectURL: (url) => revoked.push(url) },
                { removeEventListener() {} },
                { removeEventListener() {} },
                {},
            );
            const element = new Lifecycle();
            const noop = () => {};
            Object.assign(element, {
                _pickedSubtitleUrls: [...urls],
                _sourceAbort: { abort: noop },
                _roundingTimers: new Set(),
                _spinnerDelayTimer: null,
                _captionClickTimer: null,
                _nativeAudioCheckTimer: null,
                _engineTried: new Set(),
                eventHandlers: new Set([() => tornDown.push("event-handlers")]),
                canvas: { removeEventListener: noop },
                classList: { remove: noop },
                player: { destroy: () => tornDown.push("player") },
                _subtitleRenderer: {
                    destroy: () => tornDown.push("subtitle-renderer"),
                },
            });
            for (const method of [
                "abortSourceRequests",
                "cancelAutoAdvance",
                "closeAllBottomMenus",
                "closeSettingsPage",
                "detachGlobalListeners",
                "releaseMediaSession",
                "resetStutterHint",
                "resetTimeline",
                "setPipUiTimer",
                "stopAmbientColorSampling",
                "stopQoeHeartbeat",
                "stopResumeSaving",
                "stopStutterMonitor",
                "syncThumbnailRotation",
                "updateControlsState",
                "updatePlayPauseIcon",
                "updateProgressBar",
            ])
                element[method] = noop;

            for (const step of steps) {
                element[step]();
                assert.deepEqual(element._pickedSubtitleUrls, []);
                assert.deepEqual(revoked, urls);
            }
            assert.equal(element.player, null);
            assert.equal(element._subtitleRenderer, null);
            assert.deepEqual(tornDown.toSorted(), [
                "event-handlers",
                "player",
                "subtitle-renderer",
            ]);
        }
    }
});

function httpSourceForTest() {
    const start = patched.indexOf("class HttpSource {");
    const end = patched.indexOf("\nasync function eA(", start);
    const cacheStart = patched.indexOf("class LRUCache {");
    const cacheEnd = patched.indexOf("\n}\n", cacheStart) + 2;
    assert.ok(start >= 0 && end > start);
    assert.ok(cacheStart >= 0 && cacheEnd > cacheStart);
    return new Function(
        "fetch",
        `const crossOriginIsolated = false;
     const QA="HttpSource", EA=2097152, iA=262144000, tA=131072;
     const g = {debug(){}, info(){}, warn(){}, error(){}};
     const oA="LRUCache";
     ${patched.slice(cacheStart, cacheEnd)}
     ${patched.slice(start, end)}
     return HttpSource;`,
    )(async (_url, options) => {
        const range = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
        assert.ok(range, "the source must retain bounded range requests");
        return new Response(
            new Uint8Array(Number(range[2]) - Number(range[1]) + 1).fill(91),
            {
                status: 206,
            },
        );
    });
}

test(
    "a demux read larger than the time goal completes through the real range reader",
    {
        timeout: 1500,
    },
    async () => {
        const Source = httpSourceForTest();
        const source = new Source("https://fixture.invalid/video", {}, 128);
        source.size = 50 * 1024 ** 2;
        source.resizeBuffer(32 * 1024 ** 2);
        source.setPlaybackBufferGoal(3600);
        assert.equal(source.playbackAheadBytes, 2 * 1024 ** 2);
        let timer;
        try {
            const bytes = await Promise.race([
                source.read(0, 3 * 1024 ** 2),
                new Promise((_resolve, reject) => {
                    timer = setTimeout(
                        () =>
                            reject(
                                new Error(
                                    "demux read stayed behind its own gate",
                                ),
                            ),
                        750,
                    );
                }),
            ]);
            assert.equal(bytes.byteLength, 3 * 1024 ** 2);
            assert.equal(new Uint8Array(bytes).at(-1), 91);
            assert.equal(source.pendingReadEnds.size, 0);
        } finally {
            clearTimeout(timer);
            source.close();
        }
    },
);

test("pending read demands are removed after failure and belong to their stream generation", async () => {
    const Source = httpSourceForTest();
    const source = new Source("https://fixture.invalid/video", {}, 128);
    const failure = new Error("fixture reader failed");
    source.streamGeneration = 7;
    source.waitForDataUnchecked = async () => {
        assert.deepEqual(
            [...source.pendingReadEnds],
            [{ end: 300, generation: 7 }],
        );
        throw failure;
    };
    await assert.rejects(source.waitForData(100, 200), failure);
    assert.equal(source.pendingReadEnds.size, 0);
    source.close();

    const held = policy(512_000);
    held.streamGeneration = 2;
    held.pendingReadEnds = new Set([{ end: 1e9, generation: 1 }]);
    held.bufferEnd = held.playbackAheadBytes;
    setTimeout(() => {
        held.closed = true;
    }, 10);
    await held.awaitPrefetchGate();
    assert.equal(
        held.closed,
        true,
        "a superseded read must not open the current gate",
    );
});

function drainHarness(flush) {
    const start = patched.indexOf(
        "  async flush(drain = false) {",
        patched.indexOf("class MoviVideoDecoder"),
    );
    const end = patched.indexOf("\n  get configuredCodec() {", start);
    const timers = new Map();
    let nextTimer = 0;
    const Decoder = new Function(
        "setTimeout",
        "clearTimeout",
        "g",
        "YA",
        `
    return class {
      _drainGeneration = 0;
      ${patched.slice(start, end)}
    };
  `,
    )(
        (callback, delay) => {
            const id = ++nextTimer;
            timers.set(id, { callback, delay });
            return id;
        },
        (id) => timers.delete(id),
        { warn() {}, error() {} },
        "fixture decoder",
    );
    const wrapper = new Decoder();
    const calls = { resets: 0, configurations: 0 };
    Object.assign(wrapper, {
        decoder: {
            decodeQueueSize: 20,
            flush,
            reset() {
                calls.resets++;
            },
            configure() {
                calls.configurations++;
            },
        },
        lastConfig: { codec: "hvc1.fixture" },
        pendingChunks: [1, 2],
        pendingFrames: [],
    });
    return {
        wrapper,
        calls,
        timers,
        expire() {
            assert.equal(timers.size, 1);
            const timer = [...timers.values()][0];
            assert.equal(timer.delay, 1200);
            timer.callback();
        },
    };
}

test("EOF hardware drains settle on decoder failure or timeout and clear their timers", async () => {
    for (const behavior of ["resolve", "reject", "stall"]) {
        const failure = new Error("decoder refused drain");
        const fixture = drainHarness(() =>
            behavior === "stall"
                ? new Promise(() => {})
                : behavior === "reject"
                  ? Promise.reject(failure)
                  : Promise.resolve(),
        );
        const draining = fixture.wrapper.flush(true);
        if (behavior === "stall") fixture.expire();
        if (behavior === "resolve") {
            await draining;
            assert.equal(fixture.calls.resets, 0);
        } else {
            await assert.rejects(
                draining,
                behavior === "reject" ? failure : /EOF decoder drain timed out/,
            );
            assert.equal(fixture.calls.resets, 1);
            assert.equal(fixture.calls.configurations, 1);
        }
        assert.equal(fixture.timers.size, 0);
        assert.deepEqual(fixture.wrapper.pendingChunks, [1, 2]);
    }
});

test("a stale EOF drain never resets a decoder owned by a newer reset or replacement", async () => {
    for (const action of ["reset", "replace"]) {
        const fixture = drainHarness(() => new Promise(() => {}));
        const draining = fixture.wrapper.flush(true);
        if (action === "reset") fixture.wrapper.reset();
        else
            fixture.wrapper.decoder = {
                reset() {
                    throw new Error("new decoder must not be reset");
                },
                configure() {
                    throw new Error("new decoder must not be reconfigured");
                },
            };
        const resets = fixture.calls.resets;
        fixture.expire();
        await assert.rejects(draining, /EOF decoder drain timed out/);
        assert.equal(fixture.calls.resets, resets);
        assert.equal(fixture.calls.configurations, 0);
        assert.equal(fixture.timers.size, 0);
    }
});

test("a stale EOF completion cannot replace the current loop-preroll sequence", () => {
    const settle = new Function("A4", EOF_SETTLED_AFTER);
    const player = { _eofFlushSeq: 4, _eofFlushSettledSeq: 4 };
    settle.call(player, 3);
    assert.equal(player._eofFlushSettledSeq, 4);
    player._eofFlushSettledSeq = 2;
    settle.call(player, 4);
    assert.equal(player._eofFlushSettledSeq, 4);
});

function timelineFixture(chapters, options = {}) {
    function element(tag = "div") {
        const classes = new Set();
        return {
            tag,
            children: [],
            style: {},
            dataset: {},
            events: {},
            classList: {
                add: (name) => classes.add(name),
                remove: (name) => classes.delete(name),
                contains: (name) => classes.has(name),
            },
            appendChild(child) {
                this.children.push(child);
            },
            addEventListener(event, handler) {
                this.events[event] = handler;
            },
            querySelector(selector) {
                return selector === "img"
                    ? this.children.find((child) => child.tag === "img")
                    : null;
            },
        };
    }
    const start = patched.indexOf("  async generateTimelineStrip(A2) {");
    const end = patched.indexOf("\n  get muted() {", start);
    const timeline = new Function(
        "document",
        "URL",
        `return ({${patched.slice(start, end)}});`,
    )({ createElement: element }, { createObjectURL: () => "blob:fixture" });
    const strip = element(),
        status = element(),
        title = element();
    const previewChoices = [],
        frameTimes = [];
    Object.assign(timeline, {
        _thumb: false,
        _storyboard: null,
        _timelineNextIndex: 0,
        _currentManualRotation: 0,
        updateTimelineCurrent() {
            this.updated = true;
        },
        player: {
            setPreviewsEnabled(enabled) {
                previewChoices.push(enabled);
            },
            getVideoTracks: () => [],
            getChapters: () => chapters,
            getDuration: () => 60,
            async getPreviewFrame(time) {
                frameTimes.push(time);
                return new Blob(["fixture"]);
            },
        },
        ...options,
    });
    const panel = {
        querySelector: (selector) =>
            ({
                ".movi-timeline-strip": strip,
                ".movi-timeline-status": status,
                ".movi-timeline-title": title,
            })[selector],
    };
    return {
        timeline,
        strip,
        status,
        title,
        previewChoices,
        frameTimes,
        panel,
    };
}

test("native chapter cards retain titles, times and seeking without decoding preview frames", async () => {
    const fixture = timelineFixture([
        { start: 0, title: "Opening" },
        { start: 30, title: "Finale" },
    ]);
    await fixture.timeline.generateTimelineStrip(fixture.panel);
    assert.deepEqual(fixture.previewChoices, [false]);
    assert.deepEqual(fixture.frameTimes, []);
    assert.equal(fixture.strip.children.length, 2);
    const card = fixture.strip.children[1];
    assert.equal(card.classList.contains("movi-timeline-noframe"), true);
    assert.deepEqual(
        card.children[0].children.map((child) => child.textContent),
        ["Finale", "0:30"],
    );
    card.events.click({ stopPropagation() {} });
    assert.equal(fixture.timeline.currentTime, 30);
    assert.equal(fixture.timeline.updated, true);
    assert.equal(fixture.timeline._timelineGenerating, false);
});

test("chapter image extraction belongs to the visible-card scheduler", async () => {
    for (const options of [{ _thumb: true }, { _storyboard: "fixture.vtt" }]) {
        const fixture = timelineFixture(
            [{ start: 0, title: "Opening" }],
            options,
        );
        await fixture.timeline.generateTimelineStrip(fixture.panel);
        assert.deepEqual(fixture.previewChoices, [true]);
        assert.deepEqual(fixture.frameTimes, []);
        assert.equal(
            fixture.strip.children[0].classList.contains(
                "movi-timeline-noframe",
            ),
            true,
        );
    }
});

test("supplied chapter artwork remains available without extracting a frame", async () => {
    const fixture = timelineFixture(
        [{ start: 0, title: "Opening", image: "blob:chapter-art" }],
        { _thumb: true },
    );
    await fixture.timeline.generateTimelineStrip(fixture.panel);
    assert.deepEqual(fixture.frameTimes, []);
    assert.equal(fixture.strip.children[0].children[0].src, "blob:chapter-art");
});

test("preview initialization never warms itself without a viewer request", () => {
    const start = patched.indexOf("  schedulePreviewWarm(A2) {");
    const end = patched.indexOf("\n  async initPreviewPipeline()", start);
    assert.ok(start > 0 && end > start);
    const Preview = new Function(
        "setTimeout",
        `return class {${patched.slice(start, end)}}`,
    )(() => {
        throw new Error("speculative preview timer");
    });
    new Preview().schedulePreviewWarm(0);
});

test("a title-only timeline with no chapters never generates the twenty-frame strip", async () => {
    const fixture = timelineFixture([]);
    await fixture.timeline.generateTimelineStrip(fixture.panel);
    assert.deepEqual(fixture.frameTimes, []);
    assert.equal(fixture.status.textContent, "No chapters");
    assert.equal(fixture.timeline._timelineComplete, true);
    assert.equal(fixture.timeline._timelineGenerating, false);
});

test("unrecognized earlier patch variants are rejected instead of migrated", () => {
    const older = patched.replace(
        GATE_AFTER,
        GATE_AFTER.replace(
            /      \/\/ A pending demux read[\s\S]+?return;/,
            "      if (!this._prefetchThrottled && !this.playbackPrefetchHeld) return;",
        ),
    );
    assert.notEqual(older, patched);
    assert.throws(() => patchMoviSource(older, VERSION), /was modified/);
});
