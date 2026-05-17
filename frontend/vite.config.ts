import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // 不 rewrite，让 FastAPI 自己处理 /api 前缀（middleware 内部 strip）
        // 这样 dev / prod 行为一致
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
