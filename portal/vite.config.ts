/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The logo and favicon live in the repository's assets/ directory (one copy, shared with
// Cognito's managed login branding).
const assets = fileURLToPath(new URL("../assets", import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@assets": assets } },
  server: { fs: { allow: [".", assets] } },
  build: {
    sourcemap: false,
    // Hashed file names: everything but index.html and config.json is cached for a year.
    assetsDir: "assets",
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
