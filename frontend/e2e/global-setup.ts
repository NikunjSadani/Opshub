import { request } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Fetches the challan upload template (its example rows are valid against the
 * e2e-bootstrapped master data) and saves it as a fixture the challan spec uploads.
 * The dev-auth shim accepts any bearer token in a local env.
 */
export default async function globalSetup() {
  const here = dirname(fileURLToPath(import.meta.url));
  const ctx = await request.newContext({
    baseURL: 'http://localhost:8000',
    extraHTTPHeaders: { Authorization: 'Bearer e2e-admin' },
  });
  const res = await ctx.get('/api/v1/challan/template.xlsx');
  if (!res.ok()) {
    throw new Error(`template download failed: HTTP ${res.status()}`);
  }
  mkdirSync(join(here, 'fixtures'), { recursive: true });
  writeFileSync(join(here, 'fixtures', 'template.xlsx'), await res.body());
  await ctx.dispose();
}
