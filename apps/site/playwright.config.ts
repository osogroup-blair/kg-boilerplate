import { defineConfig } from "@playwright/test";

// Runs against the built site (`npm run build`), served by `astro preview` on :4321. CHROMIUM_PATH lets a machine without Playwright's own browser use an installed Chromium.
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 45_000,
  workers: 2,
  use: { baseURL: "http://localhost:4321", launchOptions: { executablePath: process.env.CHROMIUM_PATH || undefined } },
  webServer: { command: "npm run preview", url: "http://localhost:4321", reuseExistingServer: true, timeout: 60_000 },
});
