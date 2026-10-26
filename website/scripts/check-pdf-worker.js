import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { patchPdfSource, VERSION } from "./patch-pdf-worker.js";
const source = await readFile(
    process.env.TGDRIVE_PDF_SOURCE ||
        new URL(
            "../node_modules/pdfjs-dist/build/pdf.worker.mjs",
            import.meta.url,
        ),
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

function imageBoundsFixture({
    width,
    height,
    inline = false,
    ignoreErrors = true,
}) {
    const patched = patchPdfSource(source, VERSION);
    const start = patched.indexOf("  async buildPaintImageXObject({");
    const end = patched.indexOf("    let optionalContent;", start);
    assert.ok(start >= 0 && end > start);
    const loads = [];
    class Dict {
        entries = new Map();
        set(key, value) {
            this.entries.set(key, value);
        }
        get(key) {
            return this.entries.get(key);
        }
    }
    const evaluator = new Function(
        "Dict",
        "ObjectLoader",
        "warn",
        `return ({
    ${patched.slice(start, end)}
      return "accepted";
    }
  });`,
    )(
        Dict,
        {
            async load(...args) {
                loads.push(args);
            },
        },
        () => {},
    );
    evaluator.options = { maxImageSize: 16777216, ignoreErrors };
    evaluator.xref = { fixture: true };
    const image = {
        dict: {
            objId: "fixture image",
            get(key) {
                return key === "W" ? width : key === "H" ? height : undefined;
            },
        },
    };
    return {
        loads,
        evaluator,
        image,
        run: () =>
            evaluator.buildPaintImageXObject({ image, isInline: inline }),
    };
}

test("missing or oversized image dimensions are rejected before payload dependencies load", async () => {
    for (const size of [
        { width: undefined, height: 10 },
        { width: 10, height: 0 },
        { width: 10000, height: 10000 },
    ]) {
        const fixture = imageBoundsFixture(size);
        assert.equal(await fixture.run(), undefined);
        assert.equal(fixture.loads.length, 0);
    }
    const strict = imageBoundsFixture({
        width: 10000,
        height: 10000,
        ignoreErrors: false,
    });
    await assert.rejects(strict.run(), /Image exceeded maximum allowed size/);
    assert.equal(strict.loads.length, 0);
});

test("accepted external images load their selected dependency graph before decode", async () => {
    const fixture = imageBoundsFixture({ width: 4096, height: 4096 });
    assert.equal(await fixture.run(), "accepted");
    assert.equal(fixture.loads.length, 1);
    const [selected, keys, xref] = fixture.loads[0];
    assert.equal(selected.get("Image"), fixture.image);
    assert.deepEqual(keys, ["Image"]);
    assert.equal(xref, fixture.evaluator.xref);
    const inline = imageBoundsFixture({ width: 1, height: 1, inline: true });
    assert.equal(await inline.run(), "accepted");
    assert.equal(inline.loads.length, 0);
});
