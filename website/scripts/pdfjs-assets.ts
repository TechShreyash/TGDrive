import { createReadStream, readFileSync, readdirSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import type { Plugin } from "vite";

const RESOURCE_DIRECTORIES = ["cmaps", "standard_fonts", "wasm", "iccs"];
const require = createRequire(import.meta.url);

/** Serve the installed PDF.js rendering resources locally in development and builds. */
export function pdfjsAssets(): Plugin {
    const packageFile = require.resolve("pdfjs-dist/package.json");
    const packageDirectory = dirname(packageFile);
    const { version } = JSON.parse(readFileSync(packageFile, "utf8")) as {
        version: string;
    };
    const upstreamVersion = version.replace(/-tgdrive\.\d+$/, "");
    const base = `static/pdfjs/${upstreamVersion}/`;

    return {
        name: "tgdrive-pdfjs-assets",
        configureServer(server) {
            server.middlewares.use((request, response, next) => {
                const path = new URL(request.url || "/", "http://localhost")
                    .pathname;
                if (!path.startsWith("/" + base)) return next();
                const [directory, filename, extra] = path
                    .slice(base.length + 1)
                    .split("/");
                if (
                    !RESOURCE_DIRECTORIES.includes(directory) ||
                    !filename ||
                    !/^[A-Za-z0-9_.-]+$/.test(filename) ||
                    filename === "." ||
                    filename === ".." ||
                    extra !== undefined
                ) {
                    response.statusCode = 404;
                    response.end();
                    return;
                }
                const file = join(packageDirectory, directory, filename);
                try {
                    if (!statSync(file).isFile())
                        throw new Error("Missing PDF resource");
                } catch {
                    response.statusCode = 404;
                    response.end();
                    return;
                }
                response.setHeader(
                    "Content-Type",
                    filename.endsWith(".wasm")
                        ? "application/wasm"
                        : filename.endsWith(".js")
                          ? "text/javascript; charset=utf-8"
                          : "application/octet-stream",
                );
                response.setHeader("X-Content-Type-Options", "nosniff");
                createReadStream(file).on("error", next).pipe(response);
            });
        },
        generateBundle() {
            for (const directory of RESOURCE_DIRECTORIES) {
                for (const entry of readdirSync(
                    join(packageDirectory, directory),
                    {
                        withFileTypes: true,
                    },
                )) {
                    if (!entry.isFile()) continue;
                    this.emitFile({
                        type: "asset",
                        fileName: `${base}${directory}/${entry.name}`,
                        source: readFileSync(
                            join(packageDirectory, directory, entry.name),
                        ),
                    });
                }
            }
        },
    };
}
