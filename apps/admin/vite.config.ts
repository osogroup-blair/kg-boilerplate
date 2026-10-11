/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the Vite proxy makes API calls same-origin, so the API needs no CORS in development.
const target = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: { port: 5273, strictPort: true, fs: { allow: ["../.."] }, proxy: { "/v1": target, "/health": target } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], css: false, exclude: ["e2e/**", "node_modules/**"] },
});
