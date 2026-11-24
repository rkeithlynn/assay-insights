/**
 * Rendered checks of the built portal (portal/e2e), in Chromium at two desktop widths.
 *
 * Run in the pinned Playwright image by `uv run scripts/check_portal_render.py` and in CI.
 * The portal is the production build (`vite build`), served by `vite preview`.
 */

import { defineConfig } from "@playwright/test";

import { PORT } from "./e2e/fixtures";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.e2e.ts",
  outputDir: process.env.PORTAL_RENDER_OUT ?? "../.portal-render/results",
  forbidOnly: true,
  retries: 0,
  workers: 2,
  reporter: [["list"]],
  use: {
    browserName: "chromium",
    colorScheme: "light",
    locale: "en-US",
    timezoneId: "UTC",
    trace: "off",
  },
  projects: [
    { name: "1280", use: { viewport: { width: 1280, height: 800 } } },
    { name: "1800", use: { viewport: { width: 1800, height: 1000 } } },
  ],
  webServer: {
    command: `npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
