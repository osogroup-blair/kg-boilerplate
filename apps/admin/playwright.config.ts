import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  workers: 1, // the specs share one workspace and the worker's fault-injection file (operations.spec arranges a poison job), so they must not interleave
  use: { baseURL: "http://localhost:5273", trace: "retain-on-failure" },
  webServer: { command: "npm run dev", url: "http://localhost:5273", reuseExistingServer: true, timeout: 60_000 },
});
