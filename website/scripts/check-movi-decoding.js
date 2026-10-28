import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
    DECODING_PATCHES,
    SOFTWARE_FLUSH_AFTER,
    SOFTWARE_RESET_AFTER,
    SOFTWARE_QUEUE_AFTER,
} from "./movi-player-decoding.js";

let original = await readFile(
    process.env.TGDRIVE_MOVI_SOURCE ||
        new URL("../node_modules/movi-player/dist/element.slim.js", import.meta.url),
    "utf8",
);
for (const [before, after] of DECODING_PATCHES)
    if (original.includes(after)) original = original.replace(after, before);
let patched = original;
for (const [before, after] of DECODING_PATCHES) {
    assert.equal(patched.split(before).length, 2, "unique reviewed decoding patch");
    patched = patched.replace(before, after);
}

test("every decoding substitution reverses to the exact source", () => {
    let reversed = patched;
    for (const [before, after] of DECODING_PATCHES)
        reversed = reversed.replace(after, before);
    assert.equal(reversed, original);
});

function resumeGate() {
    const start = patched.indexOf("      const A3 = !(!this.trackManager.getActiveAudioTrack()");
    assert.ok(start > 0);
    const line = patched.slice(start, patched.indexOf("\n", start));
    return new Function(
        "MoviPlayer",
        `return function () { ${line} return { bound: t3, videoReady: D3, ready: F3 }; }`,
    )({ BOUND_RESUME_CUSHION_S: 0.2, SEEK_RESUME_QUEUE_ESCAPE_MS: 400, BOUND_RESUME_ESCAPE_MS: 15000 });
}
function bufferingSource(hasPicture, frames = 0, sound = 2) {
    return {
        hasPicture,
        trackManager: { getActiveAudioTrack: () => ({}), getActiveVideoTrack: () => hasPicture ? { frameRate: 30 } : null },
        noSoundLeftToWaitFor: () => false,
        activeAudioIsHeavySoftware: () => false,
        audioDecoder: { usesSoftware: false },
        audioRenderer: { getBufferedDuration: () => sound, isDroppingAudio: () => false },
        videoRenderer: { getQueueSize: () => frames },
        pictureIsPlayback: () => false,
        isInAudioOnlyTail: () => false,
        _bindAV: true,
        _bufferingSelfInflicted: true,
        _bufferingEntryTime: performance.now(),
    };
}
test("audio-only resumes on real sound without waiting for nonexistent video", () => {
    assert.deepEqual(resumeGate().call(bufferingSource(false)), { bound: false, videoReady: true, ready: true });
    assert.equal(resumeGate().call(bufferingSource(false, 0, 0)).ready, false);
});
test("sound and picture still resume together when the source has both", () => {
    assert.deepEqual(resumeGate().call(bufferingSource(true)), { bound: true, videoReady: false, ready: false });
    assert.equal(resumeGate().call(bufferingSource(true, 6)).ready, true);
});

const sendStart = patched.indexOf("  sendPacket(A2, I2, g2, C2, B2) {");
const sendMethod = patched.slice(sendStart, patched.indexOf("  receiveFrame(A2)", sendStart));
const PacketBindings = new Function(`return class { ${sendMethod} }`)();
test("EOS reaches the existing WASM ABI without allocating a zero-length packet", () => {
    const binding = new PacketBindings();
    let args;
    binding.contextPtr = 123;
    binding.module = { _malloc: () => assert.fail("EOS must not allocate"), _movi_send_packet: (...values) => { args = values; return 0; } };
    assert.equal(binding.sendPacket(2, new Uint8Array(0), -1, -1, false), 0);
    assert.deepEqual(args, [123, 2, 0, 0, -1, -1, 0]);
});
test("nonempty packets still reject allocation failure before writing WASM memory", () => {
    const binding = new PacketBindings();
    binding.contextPtr = 123;
    binding.module = { _malloc: () => 0, _movi_send_packet: () => assert.fail("OOM must not decode") };
    assert.equal(binding.sendPacket(2, new Uint8Array([1]), 0, 0, true), -6);
});

function softwareDecoder(clock = performance, Channel = MessageChannel) {
    const Decoder = new Function(
        "performance", "MessageChannel", "g", "uA",
        `return class { ${SOFTWARE_FLUSH_AFTER} ${SOFTWARE_RESET_AFTER} ${SOFTWARE_QUEUE_AFTER} }`,
    )(clock, Channel, { error() {} }, "SoftwareVideoDecoder");
    const decoder = new Decoder();
    Object.assign(decoder, { isConfigured: true, trackIndex: 0, packetQueue: [], isProcessingQueue: false, lastProcessedTimestamp: 9, processDecodedFrame() {} });
    return decoder;
}
test("software EOF finishes pending packets before draining delayed B-frames", async () => {
    const decoder = softwareDecoder();
    const events = [];
    let remaining = 2;
    decoder.packetQueue = [{}];
    decoder.decodeInternal = () => events.push("packet");
    decoder.bindings = {
        sendPacket: (_track, bytes) => { assert.equal(bytes.length, 0); events.push("EOS"); return 0; },
        receiveFrame: () => remaining-- > 0 ? 0 : -541478725,
        getFramePts: () => 4.9,
        flushDecoder: () => events.push("reset"),
    };
    decoder.processDecodedFrame = (timestamp) => { assert.equal(timestamp, 4900000); events.push("frame"); };
    await decoder.flush(true);
    assert.deepEqual(events, ["packet", "EOS", "frame", "frame", "reset"]);
    assert.equal(decoder.lastProcessedTimestamp, -1);
});
test("seek and close cancel an old drain without touching the replacement decoder", async () => {
    for (const cancel of ["flush", "reset", "close"]) {
        const decoder = softwareDecoder();
        let resets = 0;
        decoder.isProcessingQueue = true;
        decoder.bindings = { sendPacket: () => assert.fail("stale drain sent EOS"), flushDecoder: () => resets++ };
        const draining = decoder.flush(true);
        if (cancel === "flush") await decoder.flush();
        else decoder[cancel]();
        await draining;
        assert.equal(resets, cancel === "close" ? 0 : 1);
        assert.deepEqual(decoder.packetQueue, []);
    }
});
test("a stuck software drain has a bounded wait", async () => {
    let calls = 0;
    const decoder = softwareDecoder({ now: () => calls++ === 0 ? 0 : 30001 });
    decoder.isProcessingQueue = true;
    await assert.rejects(decoder.flush(true), /drain timed out/);
});
test("reconfiguration invalidates an in-flight drain before replacing its stream", async () => {
    const start = patched.indexOf("  async configure(A2, I2 = 0) {\n    ++this._flushGeneration;");
    const method = patched.slice(start, patched.indexOf("  setNonRefSkip(A2)", start));
    const configure = new Function("g", "uA", `return ({ ${method} }).configure`)(
        { info() {}, error() {} }, "SoftwareVideoDecoder",
    );
    const decoder = softwareDecoder();
    decoder.isProcessingQueue = true;
    decoder.bindings = {
        sendPacket: () => assert.fail("old drain reached the reconfigured stream"),
        enableDecoder: () => 0,
        setSkipFrame() {},
    };
    const draining = decoder.flush(true);
    await configure.call(decoder, { id: 2 });
    await draining;
    assert.equal(decoder.trackIndex, 2);
    assert.equal(decoder.isConfigured, true);
});
test("software seeks reset timestamp throttling and discard pre-seek packets", async () => {
    const decoder = softwareDecoder();
    let resets = 0;
    decoder.packetQueue = [{}];
    decoder.bindings = { flushDecoder: () => resets++ };
    await decoder.flush();
    assert.equal(decoder.lastProcessedTimestamp, -1);
    assert.deepEqual(decoder.packetQueue, []);
    assert.equal(resets, 1);
});
test("software queue ports close after success and decoder failure", async () => {
    for (const fail of [false, true]) {
        let closed = 0;
        class Channel {
            port1 = { close: () => closed++, onmessage: null };
            port2 = { close: () => closed++, postMessage: () => queueMicrotask(() => this.port1.onmessage?.(null)) };
        }
        const decoder = softwareDecoder(performance, Channel);
        decoder.packetQueue = [{}];
        decoder.decodeInternal = () => { if (fail) throw new Error("decode failed"); };
        await decoder.processQueue();
        assert.equal(closed, 2);
        assert.equal(decoder.isProcessingQueue, false);
    }
});

const capStart = patched.indexOf("  videoQueueCapFrames(A2, I2, g2) {");
const capMethod = patched.slice(capStart, patched.indexOf("  hasPictureToCarryARewind(A2)", capStart));
function cap({ width = 1920, height = 1080, fps = 30, mobile = false, memory = 8, software = true, rate = 1, postSeek = false } = {}) {
    const Core = new Function("MoviPlayer", "navigator", `return class { ${capMethod} }`)({ _isMobileDevice: mobile, BOUND_RESUME_CUSHION_S: 0.2 }, { deviceMemory: memory });
    const core = new Core();
    core.trackManager = { getActiveVideoTrack: () => ({ width, height, frameRate: fps }) };
    return core.videoQueueCapFrames(postSeek, software, rate);
}
test("decoded software pictures obey a separate byte-aware budget at every rate", () => {
    assert.equal(cap(), 16);
    assert.equal(cap({ memory: 4 }), 8);
    assert.equal(cap({ mobile: true }), 8);
    assert.equal(cap({ width: 3840, height: 2160 }), 16);
    assert.equal(cap({ rate: 4 }), 16);
    assert.equal(cap({ width: 640, height: 360 }), 60);
});
test("frame caps keep the resume cushion reachable and retain calibrated 4K hardware limits", () => {
    for (const fps of [24, 30, 60, 120])
        assert.ok(cap({ fps, mobile: true }) >= Math.ceil(fps * 0.2));
    assert.equal(cap({ width: 3840, height: 2160, software: false }), 48);
    assert.equal(cap({ width: 3840, height: 2160, software: false, mobile: true }), 16);
    assert.equal(cap({ width: 7680, height: 4320, software: false }), 16);
    assert.equal(cap({ software: false }), 100);
});
test("burst admission observes live decoded pictures while preserving audio read-ahead", () => {
    const admission = DECODING_PATCHES.find(([before]) => before.startsWith("if (!q2 && !W2"))[1];
    const blocked = new Function("M2", "W2", "q2", "MoviPlayer", `
        const l2=1000, B4=false, S2=500, N2=16;
        ${admission} return true;
        return false;
    `);
    const player = { videoDecoder: { queueSize: 0 }, videoRenderer: { getQueueSize: () => 17 }, audioDecoder: { queueSize: 0 }, trackManager: { getActiveVideoTrack: () => ({ width: 1920, height: 1080 }) } };
    assert.equal(blocked.call(player, true, false, false, {}), true);
    assert.equal(blocked.call(player, false, false, false, { _isMobileDevice: true }), true);
    assert.equal(blocked.call(player, true, true, false, {}), false);
    assert.equal(blocked.call(player, true, false, true, {}), false);
    assert.equal(blocked.call(player, false, false, false, {}), false);
    player.trackManager.getActiveVideoTrack = () => ({ width: 3840, height: 2160 });
    assert.equal(blocked.call(player, false, false, false, { _isMobileDevice: true }), false);
});
function pausedCore() {
    const start = patched.indexOf("  pauseBufferTick = async () => {");
    const method = patched.slice(start, patched.indexOf("  async releaseWakeLock()", start));
    const limits = {};
    for (const name of ["MAX_PACKETS", "VIDEO_FRAMES", "AUDIO_SECONDS"]) {
        const key = "PAUSE_BUFFER_" + name;
        const match = patched.match(new RegExp("static " + key + " = ([\\d.e]+);"));
        assert.ok(match, "read the actual shipped pause limit: " + key);
        limits[key] = Number(match[1]);
    }
    class Source {}
    const Core = new Function("MoviPlayer", "HttpSource", "g", "Je", `return class { ${method} }`)(
        limits, Source, { debug() {}, error() {} }, "MoviPlayer",
    );
    return { core: new Core(), limits };
}
test("paused packet bytes stop further reads without dropping the crossing packet", async () => {
    const { core } = pausedCore();
    let reads = 0, stops = 0;
    Object.assign(core, {
        pendingPrebufferPackets: [],
        stateManager: { getState: () => "paused" },
        stopPauseBuffering: () => stops++,
        trackManager: { getActiveVideoTrack: () => ({ id: 0 }), getActiveAudioTrack: () => ({ id: 1 }), isActiveStream: () => true },
        demuxer: { readPacket: async () => { reads++; return { streamIndex: 0, data: { byteLength: 9 * 1048576 } }; } },
    });
    await core.pauseBufferTick();
    assert.equal(reads, 2);
    assert.equal(core.pendingPrebufferPackets.length, 2);
    await core.pauseBufferTick();
    assert.equal(reads, 2);
    assert.equal(stops, 1);
    assert.equal(core.demuxInFlight, false);
});
test("pause demuxing stops at a resume cushion rather than minutes of packets", async () => {
    const { core, limits } = pausedCore();
    assert.equal(limits.PAUSE_BUFFER_AUDIO_SECONDS, 5);
    assert.equal(limits.PAUSE_BUFFER_VIDEO_FRAMES, 60);
    let reads = 0, stops = 0;
    Object.assign(core, {
        pendingPrebufferPackets: [
            ...Array.from({ length: 60 }, () => ({ streamIndex: 0, data: new Uint8Array(1) })),
            { streamIndex: 1, duration: 5, data: new Uint8Array(1) },
        ],
        stateManager: { getState: () => "paused" },
        stopPauseBuffering: () => stops++,
        trackManager: { getActiveVideoTrack: () => ({ id: 0 }), getActiveAudioTrack: () => ({ id: 1 }) },
        demuxer: { readPacket: async () => { reads++; throw new Error("unneeded pause read"); } },
    });
    await core.pauseBufferTick();
    assert.equal(reads, 0);
    assert.equal(stops, 1);
    assert.equal(core.pendingPrebufferPackets.length, 61);
});
test("decoder stats do not present an unverified WebCodecs backend as hardware", () => {
    const statement = DECODING_PATCHES.at(-2)[1];
    const stats = new Function(statement);
    assert.equal(stats.call({ useSoftware: false, hardwareUnavailable: false }).decoderType, "WebCodecs (acceleration unverified)");
    assert.equal(stats.call({ useSoftware: false, hardwareUnavailable: true }).decoderType, "Software (WebCodecs)");
    assert.equal(stats.call({ useSoftware: true }).decoderType, "Software (FFmpeg)");
});
