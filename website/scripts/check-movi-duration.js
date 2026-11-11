import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { patchMoviSource, VERSION } from "./patch-movi-player.js";
import {
    DURATION_RESCAN_PATCHES,
    patchDurationRescan,
} from "./movi-player-duration.ts";

const installed = await readFile(
    process.env.TGDRIVE_MOVI_SOURCE ||
        new URL(
            "../node_modules/movi-player/dist/element.slim.js",
            import.meta.url,
        ),
    "utf8",
);
const historical = patchMoviSource(installed, VERSION);
const source = patchDurationRescan(historical);

function method(name, start) {
    const begin = source.indexOf(`  ${name}(`, start);
    assert.ok(begin >= 0, name);
    const next = /^  (?:static )?(?:async )?(?:get |set )?[\w$]+\(/m.exec(
        source.slice(begin + 3),
    );
    return source.slice(begin, begin + 3 + next.index);
}

test("the build overlay reverses exactly to the historical published recipe output", () => {
    let reversed = source;
    for (const [before, after] of [...DURATION_RESCAN_PATCHES].reverse()) {
        assert.equal(reversed.split(after).length, 2);
        reversed = reversed.replace(after, before);
    }
    assert.equal(reversed, historical);
    assert.equal(patchMoviSource(reversed, VERSION), historical);
    assert.throws(
        () => patchDurationRescan(historical + " "),
        /exact reviewed/,
    );
    assert.throws(() => patchDurationRescan(source), /exact reviewed/);
});

const demuxStart = source.indexOf("class Demuxer");
const DurationDemuxer = new Function(
    "NA",
    `return class Demuxer {
    ${method("static durationRescanBudgetMs", demuxStart)}
    ${method("static isLocallyBacked", demuxStart)}
}`,
)(64 * 1024 ** 2);
const coreStart = source.indexOf("class MoviPlayer extends");
assert.ok(demuxStart >= 0 && coreStart >= 0);

class ScanFile {
    closes = 0;
    reads = 0;
    sizes = 0;
    secondary = false;
    markSecondary() {
        this.secondary = true;
    }
    async getSize() {
        this.sizes++;
        assert.ok(this.secondary);
        return 4;
    }
    async read() {
        this.reads++;
        return Uint8Array.of(1, 2, 3, 4).buffer;
    }
    close() {
        this.closes++;
        assert.ok(this.secondary);
    }
}

function durationPlayer({
    key = "file:clip.mkv:4:0",
    size = 4,
    reader = new ScanFile(),
    createSource = async () => reader,
    module = async () => ({}),
} = {}) {
    const bindings = [],
        budgets = [],
        events = [];
    class ScanBindings {
        constructor() {
            bindings.push(this);
            this.destroyed = false;
        }
        create() {
            return true;
        }
        setDataSource(value) {
            this.dataSource = value;
        }
        async open() {
            this.size = await this.dataSource.getSize();
        }
        async scanDuration(budget) {
            budgets.push(budget);
            this.bytes = await this.dataSource.read(0, 4);
            return 60;
        }
        destroy() {
            this.destroyed = true;
        }
    }
    const Core = new Function(
        "Demuxer",
        "FileSource",
        "gA",
        "WasmBindings",
        "g",
        "Je",
        `return class {
        ${method("async rescanDurationInBackground", coreStart)}
        ${method("destroyPreviewPipeline", coreStart)}
    }`,
    )(
        DurationDemuxer,
        ScanFile,
        module,
        ScanBindings,
        { debug() {}, info() {}, warn() {} },
        "rescan",
    );
    const player = new Core();
    Object.assign(player, {
        _destroyed: false,
        _previewGeneration: 0,
        _durationRescanDone: false,
        _durationRescanSource: null,
        _lifetimeAbort: new AbortController(),
        config: { source: { type: "file", file: {} } },
        source: { getKey: () => key },
        fileSize: size,
        mediaInfo: { duration: 0 },
        startTime: 2,
        createSource,
        clock: {
            setDuration(value) {
                events.push(["clock", value]);
            },
        },
        emit(...args) {
            events.push(args);
        },
        releaseSharedThumbModule() {},
    });
    return { player, reader, bindings, budgets, events };
}

test("duration recovery treats every HTTP source as network, including loopback and relative URLs", () => {
    for (const key of [
        "http://localhost/file/movie",
        "https://127.0.0.1/file/movie",
        "http://[::1]/file/movie",
        "/file/movie",
        "https://media.test/movie",
        "http://localhost/file?source=https%3A%2F%2Fmedia.test%2Fmovie",
        "data:video/mp4;base64,AA==",
    ]) {
        assert.equal(DurationDemuxer.isLocallyBacked(key), false, key);
        assert.equal(
            DurationDemuxer.durationRescanBudgetMs(key, 64 * 1024 ** 2),
            60_000,
        );
        assert.equal(
            DurationDemuxer.durationRescanBudgetMs(key, 64 * 1024 ** 2 + 1),
            null,
        );
    }
    for (const key of ["file:clip.mkv:4:0", "blob:https://localhost/owned"]) {
        assert.equal(DurationDemuxer.isLocallyBacked(key), true);
        assert.equal(DurationDemuxer.durationRescanBudgetMs(key, 1), 10_000);
        assert.equal(
            DurationDemuxer.durationRescanBudgetMs(key, 1.5 * 1024 ** 3),
            120_000,
        );
        assert.equal(
            DurationDemuxer.durationRescanBudgetMs(key, 1.5 * 1024 ** 3 + 1),
            null,
        );
    }
    for (const size of [0, -1, NaN, Infinity])
        assert.equal(
            DurationDemuxer.durationRescanBudgetMs("file:clip", size),
            null,
        );
    assert.equal(
        DurationDemuxer.durationRescanBudgetMs("hls-segments:clip", 4),
        null,
    );
});

test("large loopback scans open no secondary source or WASM context", async () => {
    const { player, reader, bindings } = durationPlayer({
        key: "http://localhost/file/telegram-backed",
        size: 1024 ** 3,
        createSource: () => assert.fail("network scan must not open a source"),
        module: () => assert.fail("network scan must not initialize WASM"),
    });
    await player.rescanDurationInBackground();
    assert.equal(reader.closes, 0);
    assert.equal(bindings.length, 0);
});

test("an already aborted player starts no duration work", async () => {
    const { player, bindings } = durationPlayer({
        createSource: () =>
            assert.fail("aborted playback must not open a source"),
    });
    player._lifetimeAbort.abort();
    await player.rescanDurationInBackground();
    assert.equal(bindings.length, 0);
});

test(
    "teardown aborts an active small HTTP scan through its owned source",
    { timeout: 1000 },
    async () => {
        const entered = Promise.withResolvers(),
            controller = new AbortController();
        let closes = 0;
        const reader = {
            getSize: async () => 4,
            read() {
                entered.resolve();
                return new Promise((_resolve, reject) => {
                    controller.signal.addEventListener(
                        "abort",
                        () => reject(controller.signal.reason),
                        { once: true },
                    );
                });
            },
            close() {
                closes++;
                controller.abort();
            },
        };
        const { player, bindings, events } = durationPlayer({
            reader,
            key: "http://localhost/file/small",
        });
        const running = player.rescanDurationInBackground();
        try {
            await entered.promise;
            player.destroyPreviewPipeline();
            assert.ok(controller.signal.aborted);
            await running;
            assert.equal(closes, 1);
            assert.ok(bindings[0].destroyed);
            assert.deepEqual(events, []);
        } finally {
            controller.abort();
            await running;
        }
    },
);

test("a bounded local rescan keeps secondary reads and publishes a missing duration", async () => {
    const { player, reader, bindings, budgets, events } = durationPlayer();
    await player.rescanDurationInBackground();
    assert.deepEqual(budgets, [10_000]);
    assert.deepEqual([...bindings[0].bytes], [1, 2, 3, 4]);
    assert.equal(reader.reads, 1);
    assert.equal(reader.closes, 1);
    assert.ok(bindings[0].destroyed);
    assert.equal(player._durationRescanSource, null);
    assert.equal(player.mediaInfo.duration, 60);
    assert.deepEqual(events, [
        ["clock", 62],
        ["durationChange", 60],
    ]);
    await player.rescanDurationInBackground();
    assert.equal(bindings.length, 1, "the one-shot rescan remains one-shot");
});

for (const reason of ["destroy", "generation", "abort"]) {
    test(
        `duration reads discard late bytes and stop future I/O after ${reason}`,
        { timeout: 1000 },
        async () => {
            const entered = Promise.withResolvers(),
                release = Promise.withResolvers();
            const reader = new ScanFile();
            reader.read = async () => {
                reader.reads++;
                entered.resolve();
                return release.promise;
            };
            const { player, bindings, events } = durationPlayer({ reader });
            const running = player.rescanDurationInBackground();
            try {
                await entered.promise;
                if (reason === "destroy") player._destroyed = true;
                if (reason !== "generation") player._lifetimeAbort.abort();
                if (reason !== "abort") {
                    player.destroyPreviewPipeline();
                    assert.equal(
                        reader.closes,
                        1,
                        "teardown closes the active source immediately",
                    );
                }
                assert.equal(
                    bindings[0].destroyed,
                    false,
                    "bindings stay owned until the read unwinds",
                );
                release.resolve(Uint8Array.of(9, 9, 9, 9).buffer);
                await running;
                assert.equal(bindings[0].bytes.byteLength, 0);
                assert.ok(bindings[0].destroyed);
                assert.equal(reader.closes, 1);
                assert.equal(
                    (await bindings[0].dataSource.read(0, 4)).byteLength,
                    0,
                );
                assert.equal(await bindings[0].dataSource.getSize(), 0);
                assert.equal(reader.reads, 1);
                assert.equal(reader.sizes, 1);
                assert.equal(player._durationRescanSource, null);
                assert.equal(player.mediaInfo.duration, 0);
                assert.deepEqual(events, []);
            } finally {
                release.resolve(new ArrayBuffer(0));
                await running;
            }
        },
    );
}

for (const stage of ["source", "module", "size"]) {
    test(
        `duration teardown owns a late ${stage} completion`,
        { timeout: 1000 },
        async () => {
            const entered = Promise.withResolvers(),
                release = Promise.withResolvers();
            const reader = new ScanFile();
            const hold = async (value) => {
                entered.resolve();
                await release.promise;
                return value;
            };
            const options = { reader };
            if (stage === "source") options.createSource = () => hold(reader);
            if (stage === "module") options.module = () => hold({});
            if (stage === "size")
                reader.getSize = async () => {
                    reader.sizes++;
                    return hold(4);
                };
            const { player, bindings, budgets, events } =
                durationPlayer(options);
            const running = player.rescanDurationInBackground();
            try {
                await entered.promise;
                player.destroyPreviewPipeline();
                assert.equal(reader.closes, stage === "source" ? 0 : 1);
                release.resolve();
                await running;
                assert.equal(reader.closes, 1);
                assert.ok(
                    reader.secondary,
                    "late FileSource cleanup must not clear playback's shared cache",
                );
                assert.equal(player._durationRescanSource, null);
                assert.deepEqual(budgets, []);
                assert.deepEqual(events, []);
                if (stage === "size") {
                    assert.equal(bindings[0].size, 0);
                    assert.ok(bindings[0].destroyed);
                } else assert.equal(bindings.length, 0);
            } finally {
                release.resolve();
                await running;
            }
        },
    );
}

test(
    "an old duration scan cannot release a newer generation's active source",
    { timeout: 1000 },
    async () => {
        const readers = [new ScanFile(), new ScanFile()];
        const entered = readers.map(() => Promise.withResolvers());
        const released = readers.map(() => Promise.withResolvers());
        readers.forEach((reader, index) => {
            reader.read = async () => {
                reader.reads++;
                entered[index].resolve();
                return released[index].promise;
            };
        });
        let next = 0;
        const { player, bindings } = durationPlayer({
            createSource: async () => readers[next++],
        });
        const old = player.rescanDurationInBackground();
        let current;
        try {
            await entered[0].promise;
            player.destroyPreviewPipeline();
            player._durationRescanDone = false;
            current = player.rescanDurationInBackground();
            await entered[1].promise;
            released[0].resolve(new ArrayBuffer(4));
            await old;
            assert.equal(player._durationRescanSource, readers[1]);
            assert.equal(readers[0].closes, 1);
            assert.equal(readers[1].closes, 0);
            assert.equal(player.mediaInfo.duration, 0);
            released[1].resolve(new ArrayBuffer(4));
            await current;
            assert.equal(readers[1].closes, 1);
            assert.equal(player._durationRescanSource, null);
            assert.equal(player.mediaInfo.duration, 60);
            assert.ok(bindings.every((binding) => binding.destroyed));
        } finally {
            for (const release of released) release.resolve(new ArrayBuffer(0));
            await Promise.all([old, current]);
        }
    },
);

test("duration read failures clean up without failing normal playback", async () => {
    const reader = new ScanFile();
    reader.read = async () => {
        throw new Error("tiny fake read failure");
    };
    const { player, bindings, events } = durationPlayer({ reader });
    await player.rescanDurationInBackground();
    assert.equal(reader.closes, 1);
    assert.ok(bindings[0].destroyed);
    assert.equal(player._durationRescanSource, null);
    assert.equal(player._destroyed, false);
    assert.deepEqual(events, []);
});
