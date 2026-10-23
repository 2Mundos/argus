import path from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development Vite serves the dashboard and proxies the API; in production the
// Argus process serves both (ARGUS_DASHBOARD_DIR). Override the API origin with
// ARGUS_API_URL (e.g. https://argus.example.com) when the UI runs against a remote.
const apiTarget = process.env.ARGUS_API_URL ?? "http://localhost:8000";
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [repoRoot] },
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/healthz": { target: apiTarget, changeOrigin: true },
    },
  },
});
