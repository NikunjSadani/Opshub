import { defineConfig, devices } from '@playwright/test';

/**
 * E2E harness: drives the real React SPA against the real FastAPI backend (fresh
 * sqlite DB, dev-auth shim, local-only stub PDF renderer). Two webServers are
 * started and torn down automatically; global setup fetches the upload template.
 *
 * Run:  npm run e2e   (headless)   ·   npm run e2e:headed
 */
export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: false, // specs share one backend DB; keep them serial + ordered
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
    actionTimeout: 10_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'node e2e/serve-backend.mjs',
      url: 'http://localhost:8000/docs', // FastAPI docs = unauthenticated readiness probe
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
