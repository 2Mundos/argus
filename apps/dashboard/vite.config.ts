import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vite injects env into the config worker; keep this file free of `@types/node`.
declare const process: { env: Record<string, string | undefined> };

// In development Vite serves the dashboard and proxies the API; in production the
// Argus process serves both (ARGUS_DASHBOARD_DIR). Override the API origin with
// ARGUS_API_URL (e.g. https://argus.example.com) when the UI runs against a remote.
const apiTarget = process.env.ARGUS_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    // Allow importing brand assets from the repo `assets/` tree.
    fs: { allow: ["../.."] },
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/healthz": { target: apiTarget, changeOrigin: true },
    },
  },
});
