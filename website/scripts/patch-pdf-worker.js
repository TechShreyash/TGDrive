import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

export const VERSION = "6.4.299";
const ORIGINAL_SHA256 =
  "07ceb740d746e5d9012fcf9f27d5c7dea09f7d2f5e74f5c8109e5787551a2333";
const NOTICE =
  "// TGDrive: load shared PDF image resources only when painted.\n";
// Some producers put every page's image in one shared resource dictionary.
// PDF.js eagerly loads all their streams, even with disableAutoFetch. Keep
// dictionary/header discovery, but defer image payloads until actually used.
export const PATCHES = [
  [
    "  makeSubStream(start, length, dict = null) {\n    if (length) {\n      if (start + length > this.progressiveDataLength) {",
    '  makeSubStream(start, length, dict = null) {\n    if (length) {\n      // Image payloads stay sparse until ObjectLoader loads the painted image.\n      if (start + length > this.progressiveDataLength && !isName(dict?.get("Subtype"), "Image")) {',
  ],
  [
    "  constructor(dict, keys, xref) {\n    this.dict = dict;",
    "  constructor(dict, keys, xref, deferImages = false) {\n    this.deferImages = deferImages;\n    this.dict = dict;",
  ],
  [
    "      if (currentNode instanceof BaseStream) {\n        const baseStreams = currentNode.getBaseStreams();",
    '      if (currentNode instanceof BaseStream) {\n        if (this.deferImages && isName(currentNode.dict.get("Subtype"), "Image")) continue;\n        const baseStreams = currentNode.getBaseStreams();',
  ],
  [
    "  static async load(obj, keys, xref) {",
    "  static async load(obj, keys, xref, deferImages = false) {",
  ],
  [
    "    const objLoader = new ObjectLoader(obj, keys, xref);",
    "    const objLoader = new ObjectLoader(obj, keys, xref, deferImages);",
  ],
  [
    "    await ObjectLoader.load(this.resources, keys, this.xref);",
    "    await ObjectLoader.load(this.resources, keys, this.xref, true);",
  ],
  [
    "    await ObjectLoader.load(localResources, keys, this.xref);",
    "    await ObjectLoader.load(localResources, keys, this.xref, true);",
  ],
  [
    '    const imageRef = dict.objId;\n    const w = dict.get("W", "Width");',
    '    // Load this image, its masks and color resources before asynchronous decode.\n    if (!isInline) {\n      const selected = new Dict(this.xref);\n      selected.set("Image", image);\n      await ObjectLoader.load(selected, ["Image"], this.xref);\n    }\n    const imageRef = dict.objId;\n    const w = dict.get("W", "Width");',
  ],
];
export function patchPdfSource(source, version) {
  if (version !== VERSION)
    throw new Error("Review the PDF lazy-image patch before upgrading PDF.js.");
  let original = source.startsWith(NOTICE)
    ? source.slice(NOTICE.length)
    : source;
  for (const [before, after] of PATCHES)
    original = original.replace(after, before);
  if (
    createHash("sha256").update(original).digest("hex") !== ORIGINAL_SHA256 ||
    PATCHES.some(([before]) => original.split(before).length !== 2)
  ) {
    throw new Error(
      "The PDF worker differs from the reviewed bundle. Run npm ci; do not patch an unknown worker.",
    );
  }
  for (const [before, after] of PATCHES)
    original = original.replace(before, after);
  return NOTICE + original;
}
export async function patchInstalledPdf() {
  const root = new URL("../", import.meta.url);
  const manifest = JSON.parse(
    await readFile(
      new URL("node_modules/pdfjs-dist/package.json", root),
      "utf8",
    ),
  );
  const path = new URL("node_modules/pdfjs-dist/build/pdf.worker.mjs", root);
  const source = await readFile(path, "utf8");
  const patched = patchPdfSource(source, manifest.version);
  if (patched !== source) await writeFile(path, patched);
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  await patchInstalledPdf();
