import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development Vite serves the dashboard and proxies the API; in production the
// Argus process serves both (ARGUS_DASHBOARD_DIR).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
