import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Proxies /api to the FastAPI trial backend so the frontend can use
// relative fetch("/api/...") calls with no CORS configuration needed in dev.
//
// Ports come from the project-root .env — the same file the backend reads:
//   API_PORT  where the backend listens        (default 8800)
//   UI_URL    where this dev server is served  (default http://localhost:5173)
// Either can be overridden for the frontend alone with VITE_API_TARGET / VITE_DEV_PORT.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, "../..", ""), ...loadEnv(mode, ".", "") };
  const uiPort = Number(new URL(env.UI_URL || "http://localhost:5173").port) || 5173;
  return {
    plugins: [react()],
    server: {
      port: Number(env.VITE_DEV_PORT) || uiPort,
      proxy: {
        "/api": env.VITE_API_TARGET || `http://localhost:${env.API_PORT || 8800}`,
      },
    },
  };
});
