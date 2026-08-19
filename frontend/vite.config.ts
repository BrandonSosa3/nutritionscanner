import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true, // reachable from a phone on the same network, for real capture testing
    port: 5173,
    // The API is same-origin in development, so the browser never makes a
    // cross-origin request and CORS never enters the picture locally. In
    // production the two deploy separately and CORS_ORIGINS covers it.
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
