import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { pdfjsAssets } from "./scripts/pdfjs-assets.ts";
export default defineConfig({
    plugins: [react(), pdfjsAssets()],
    build: { outDir: "dist", emptyOutDir: true },
    server: {
        proxy: {
            "/api": "http://127.0.0.1:8000",
            "/file": "http://127.0.0.1:8000",
            "/stream": "http://127.0.0.1:8000",
        },
    },
});
