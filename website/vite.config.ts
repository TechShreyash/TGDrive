import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { pdfjsAssets } from "./scripts/pdfjs-assets.ts";

// Keep the browser's Host so the backend's same-origin check accepts dev requests.
const backend = { target: "http://127.0.0.1:8000", changeOrigin: false };

export default defineConfig({
    plugins: [react(), pdfjsAssets()],
    build: { outDir: "dist", emptyOutDir: true },
    server: {
        proxy: {
            "/api": backend,
            "/file": backend,
            "/stream": backend,
        },
    },
});
