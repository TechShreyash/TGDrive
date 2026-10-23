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
  digest,
} from "./patch-movi-player.js";

const installed = await readFile(
  new URL("../node_modules/movi-player/dist/element.slim.js", import.meta.url),
  "utf8",
);
let original = installed.startsWith(NOTICE)
  ? installed.slice(NOTICE.length)
  : installed;
for (const [before, after] of PATCHES)
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
  source.setPlaybackBufferGoal(0, duration, rate * 8);
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
test("variable bitrate observations increase the time buffer without following seeks", () => {
  const source = policy(100_000);
  source.position = 500_000;
  source.setPlaybackBufferGoal(1, 600, 800_000);
  assert.equal(source.playbackAheadBytes, 45_000_000);
  source.position = 59_000_000;
  source.setPlaybackBufferGoal(2, 600, 800_000, true);
  assert.equal(source.playbackRateSample, null);
  source.setPlaybackBufferGoal(400, 600, 800_000);
  assert.equal(source.playbackAheadBytes, 45_000_000);
});
test("EOF drains queued hardware frames without recreating the HEVC decoder", async () => {
  const patched = patchMoviSource(original, VERSION);
  const start = patched.indexOf("  async flush(drain = false) {");
  const method = patched.slice(start, patched.indexOf("\n  reset() {", start));
  const decoder = new Function(`return ({${method}})`)();
  let drains = 0;
  Object.assign(decoder, {
    decoder: {
      async flush() {
        drains++;
      },
    },
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
    () => patchMoviSource(patchMoviSource(original, VERSION) + " ", VERSION),
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
