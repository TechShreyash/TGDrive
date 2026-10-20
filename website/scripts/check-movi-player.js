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
  digest,
} from "./patch-movi-player.js";

const installed = await readFile(
  new URL("../node_modules/movi-player/dist/element.slim.js", import.meta.url),
  "utf8",
);
const original = installed.startsWith(NOTICE)
  ? installed.slice(NOTICE.length).replace(AFTER, BEFORE)
  : installed;

test("the pinned bundle has exactly the reviewed source identity", () => {
  assert.equal(digest(original), ORIGINAL_SHA256);
  assert.equal(original.split(BEFORE).length, 2);
});
test("the portable patch is deterministic and idempotent", () => {
  const patched = patchMoviSource(original, VERSION);
  assert.ok(patched.startsWith(NOTICE));
  assert.equal(patched.split(AFTER).length, 2);
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
