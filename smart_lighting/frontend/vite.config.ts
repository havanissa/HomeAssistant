import { defineConfig } from "vite";

// Relative asset URLs (base "./") so the interface works under any
// Home Assistant Ingress prefix. The build goes straight into the backend
// package, which the App's Dockerfile copies into the image.
export default defineConfig({
  base: "./",
  build: {
    outDir: "../backend/smart_lighting/static",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 600,
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8099", ws: true },
    },
  },
});
