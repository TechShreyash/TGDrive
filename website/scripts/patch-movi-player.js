import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

// Movi 0.4.1 can resume a seek on video alone on high-latency audio outputs.
// Late audio then pulls playback back to the requested seek position. Reuse
// its coordinated buffering path until playable sound is actually ready.
export const VERSION = "0.4.1";
export const ORIGINAL_SHA256 =
  "834efaa395f82503fdea3538e03c2ad83f01a7fc409ca93d93dea500affa63c7";
export const BEFORE = "if (this.needsSeekResumeQueue()) return";
export const AFTER =
  "if (this.needsSeekResumeQueue() || (!this.audioRenderer.isDroppingAudio() && !this.noSoundLeftToWaitFor() && this.audioSideStillPriming())) return";
export const NOTICE =
  "/*! Modified by TGDrive (2026-10-20): coordinate seek resume with playable audio. See third-party/movi-player-TGDrive-patch-NOTICE.txt. */\n";
export const digest = (source) =>
  createHash("sha256").update(source).digest("hex");

export function patchMoviSource(source, version) {
  if (version !== VERSION)
    throw new Error(
      `TGDrive's seek fix requires movi-player ${VERSION}; found ${version}. Review the upstream fix before updating the pinned player.`,
    );
  if (source.startsWith(NOTICE)) {
    const original = source.slice(NOTICE.length).replace(AFTER, BEFORE);
    if (
      digest(original) === ORIGINAL_SHA256 &&
      source === NOTICE + original.replace(BEFORE, AFTER)
    )
      return source;
    throw new Error(
      "TGDrive's installed Movi seek patch was modified. Run npm ci and rebuild.",
    );
  }
  if (digest(source) !== ORIGINAL_SHA256 || source.split(BEFORE).length !== 2)
    throw new Error(
      "The pinned Movi bundle does not match TGDrive's reviewed seek fix. Run npm ci; do not apply the patch to an unknown bundle.",
    );
  return NOTICE + source.replace(BEFORE, AFTER);
}

export async function patchInstalledMovi() {
  const root = new URL("../", import.meta.url);
  const manifest = JSON.parse(
    await readFile(
      new URL("node_modules/movi-player/package.json", root),
      "utf8",
    ),
  );
  const bundle = new URL("node_modules/movi-player/dist/element.slim.js", root);
  const source = await readFile(bundle, "utf8");
  const patched = patchMoviSource(source, manifest.version);
  if (patched !== source) await writeFile(bundle, patched);
  console.info(
    `[TGDrive] Movi ${VERSION} coordinated-audio seek fix verified.`,
  );
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  await patchInstalledMovi();
}
