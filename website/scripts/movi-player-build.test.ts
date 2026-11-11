import { afterEach, describe, expect, it, vi } from "vitest";
import {
    mkdtempSync,
    readFileSync,
    realpathSync,
    rmSync,
    symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { normalizePath } from "vite";
import { moviPlayerBuild } from "./movi-player-build.ts";
import { patchMoviSource, VERSION } from "./patch-movi-player.js";
import { patchDurationRescan } from "./movi-player-duration.ts";

vi.mock("node:fs", async (importOriginal) => {
    const actual = await importOriginal<typeof import("node:fs")>();
    return { ...actual, readFileSync: vi.fn(actual.readFileSync) };
});

const entry = normalizePath(
    realpathSync(
        fileURLToPath(import.meta.resolve("movi-player/element/slim")),
    ),
);
const installed = readFileSync(entry, "utf8");
const historical = patchMoviSource(installed, VERSION);
const patched = patchDurationRescan(historical);
const temporaryDirectories: string[] = [];
afterEach(() => {
    vi.clearAllMocks();
    for (const path of temporaryDirectories.splice(0))
        rmSync(path, { recursive: true, force: true });
});

describe("reviewed Movi build transform", () => {
    it("keeps the published recipe output byte-identical before the build-only overlay", () => {
        expect(historical).toBe(installed);
        expect(patched).not.toBe(historical);
    });
    it("pins the ESM slim entry for build/dev and excludes dependency prebundling", () => {
        const plugin = moviPlayerBuild();
        expect(plugin.enforce).toBe("pre");
        expect(plugin.resolveId("movi-player/element/slim")).toBe(entry);
        expect(entry.endsWith("/element.slim.js")).toBe(true);
        expect(plugin.resolveId("movi-player/movi.wasm")).toBeNull();
        expect(plugin.config()).toEqual({
            optimizeDeps: { exclude: ["movi-player/element/slim"] },
        });
    });

    it.each([
        ["production", entry],
        ["versioned dev", entry + "?v=reviewed"],
        ["timestamped dev", entry + "?t=123"],
        ["filesystem dev", "/@fs" + entry],
    ])(
        "applies the same recipe to %s without writing the package",
        (_name, id) => {
            const plugin = moviPlayerBuild();
            expect(plugin.transform(installed, id)).toEqual({
                code: patched,
                map: null,
            });
            expect(readFileSync(entry, "utf8")).toBe(installed);
        },
    );

    it("recognizes a real symlink to the installed entry", () => {
        const directory = mkdtempSync(join(tmpdir(), "tgdrive-movi-build-"));
        temporaryDirectories.push(directory);
        const alias = join(directory, "linked-package");
        symlinkSync(dirname(entry), alias, "dir");
        const plugin = moviPlayerBuild();
        expect(
            plugin.transform(
                installed,
                join(alias, "element.slim.js") + "?v=one",
            ),
        ).toEqual({ code: patched, map: null });
    });

    it("ignores unrelated modules, virtual modules and non-executable raw/url imports", () => {
        const plugin = moviPlayerBuild();
        for (const id of [
            entry + "?raw",
            entry + "?url",
            "\0" + entry,
            join(dirname(entry), "element.slim.cjs"),
            "/missing-package/element.slim.js",
            "/src/player.ts",
        ])
            expect(plugin.transform("unrelated source", id)).toBeNull();
    });

    it("rejects changed source before applying a patch", () => {
        const plugin = moviPlayerBuild();
        expect(() => plugin.transform(installed + " ", entry)).toThrow(
            /differs from the reviewed package/,
        );
        expect(() => plugin.transform(patched, entry)).toThrow(
            /differs from the reviewed package/,
        );
    });

    it("rejects changed package versions before serving or building", () => {
        vi.mocked(readFileSync).mockReturnValueOnce(
            JSON.stringify({ version: "0.4.2" }),
        );
        expect(() => moviPlayerBuild()).toThrow(/Review the Movi build patch/);
    });
});
