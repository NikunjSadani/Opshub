/// <reference types="vitest" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The SPA is served same-origin from the FastAPI service in prod (one Cloud Run
// service, no CORS). `build.outDir` = `dist` is what FastAPI mounts as static.
// In local dev we proxy `/api` to the FastAPI dev server so the same relative
// URLs (`/api/v1/...`) work in every environment.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    css: true,
  },
});
