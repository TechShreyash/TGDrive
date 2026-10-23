import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { patchPdfSource, VERSION } from "./patch-pdf-worker.js";
const source = await readFile(
  new URL("../node_modules/pdfjs-dist/build/pdf.worker.mjs", import.meta.url),
  "utf8",
);
test("PDF patch is idempotent on the exact pinned worker", () => {
  const patched = patchPdfSource(source, VERSION);
  assert.equal(patchPdfSource(patched, VERSION), patched);
});
test("PDF patch rejects unknown versions and modified source", () => {
  assert.throws(() => patchPdfSource(source, "0.0.0"));
  assert.throws(() => patchPdfSource(source + "\nmodified", VERSION));
});
