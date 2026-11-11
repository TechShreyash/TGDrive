import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { normalizePath, type Plugin } from "vite";
import { patchMoviSource, VERSION } from "./patch-movi-player.js";
import { patchDurationRescan } from "./movi-player-duration.ts";

const SLIM_MODULE = "movi-player/element/slim";
const INSTALLED_VERSION = "0.4.1-tgdrive.3";
const INSTALLED_SHA256 =
    "f14123275c415393e88a5cfed2c529c23c6d0a5ed66d56d8de5bd0c4ff8fb59f";

/** Apply the reviewed recipe in memory; the locked npm package stays untouched. */
export function moviPlayerBuild() {
    const entry = normalizePath(
        realpathSync(fileURLToPath(import.meta.resolve(SLIM_MODULE))),
    );
    const { version } = JSON.parse(
        readFileSync(join(dirname(entry), "..", "package.json"), "utf8"),
    ) as { version: unknown };
    if (version !== INSTALLED_VERSION || basename(entry) !== "element.slim.js")
        throw new Error(
            `Review the Movi build patch before changing ${INSTALLED_VERSION}.`,
        );

    return {
        name: "tgdrive-movi-player-build",
        enforce: "pre",
        config() {
            return { optimizeDeps: { exclude: [SLIM_MODULE] } };
        },
        resolveId(id: string) {
            if (id === SLIM_MODULE) return entry;
            return null;
        },
        transform(code: string, id: string) {
            if (id.startsWith("\0")) return null;
            const [requestPath, search] = id.split("?", 2);
            const query = new URLSearchParams(search?.split("#", 1)[0]);
            if (query.has("raw") || query.has("url")) return null;
            let path = normalizePath(
                requestPath.split("#", 1)[0].replace(/^\/@fs(?=\/)/, ""),
            );
            if (path !== entry) {
                if (basename(path) !== "element.slim.js") return null;
                try {
                    path = normalizePath(realpathSync(path));
                } catch {
                    return null;
                }
            }
            if (path !== entry) return null;
            if (
                createHash("sha256").update(code).digest("hex") !==
                INSTALLED_SHA256
            )
                throw new Error(
                    "The installed Movi slim source differs from the reviewed package. Run npm ci; review any viewer upgrade before building.",
                );
            return {
                code: patchDurationRescan(patchMoviSource(code, VERSION)),
                map: null,
            };
        },
    } satisfies Plugin;
}
