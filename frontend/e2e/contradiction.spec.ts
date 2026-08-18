import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const TEMPLATE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'template.xlsx');
const NEW = '/m/document_automation/new';
// The dev-auth shim (local env) accepts any bearer token and resolves to the seeded
// ADMIN — the same header global-setup uses. `page.request` is same-origin (baseURL
// :5173) so `/api/v1/...` is proxied by Vite to the FastAPI backend.
const AUTH = { headers: { Authorization: 'Bearer e2e-admin' } };
const PARTIES = '/api/v1/masterdata/consignee-parties';

/**
 * The headline consignee CONTRADICTION-REVIEW flow (inc 16), end-to-end and
 * self-contained: it manufactures its own contradiction by editing a stored
 * golden record so a re-upload of the SAME template disagrees on that GSTIN.
 *
 *  1. Upload -> validate -> generate the template. Its first-ever generation
 *     stores the consignee golden-record parties from its rows.
 *  2. Via the API, rename the first stored party (GSTIN is immutable; name is
 *     editable) so the record now diverges from what the template will upload.
 *  3. Re-upload the SAME template -> the batch goes to NEEDS_REVIEW (the uploaded
 *     name contradicts the just-edited stored name) and the ReviewPanel appears
 *     inline in New Challan.
 *  4. Resolve every contradiction with "Reject" (keep our stored value) and save.
 *  5. The batch flips to VALIDATED -> generate -> COMPLETED artifacts.
 */
test.describe('Consignee contradiction review', () => {
  test('generate -> edit master -> re-upload -> NEEDS_REVIEW -> resolve -> generate', async ({
    page,
  }) => {
    // --- 1. Upload + validate + generate so the golden-record parties exist. ---
    await page.goto(NEW);
    await page.setInputFiles('#challan-file', TEMPLATE);
    await page.getByRole('button', { name: 'Upload & validate' }).click();
    await page.getByRole('button', { name: /Generate \d+ challans?/ }).click();
    await expect(page.getByRole('button', { name: 'Download ZIP' })).toBeEnabled({
      timeout: 20_000,
    });

    // --- 2. Rename the first stored consignee party via the API. ---
    const listRes = await page.request.get(PARTIES, AUTH);
    expect(listRes.ok()).toBeTruthy();
    const parties = (await listRes.json()) as Array<{ id: number; gstin: string; name: string }>;
    expect(parties.length).toBeGreaterThan(0);
    const target = parties[0];
    const patchRes = await page.request.patch(`${PARTIES}/${target.id}`, {
      ...AUTH,
      data: { name: 'E2E Changed Name Pvt Ltd' },
    });
    expect(patchRes.ok()).toBeTruthy();
    expect((await patchRes.json()).name).toBe('E2E Changed Name Pvt Ltd');

    // --- 3. Re-upload the SAME template -> NEEDS_REVIEW (fresh mount). ---
    await page.goto(NEW);
    await page.setInputFiles('#challan-file', TEMPLATE);
    await page.getByRole('button', { name: 'Upload & validate' }).click();

    // The ReviewPanel renders inline (uploaded.status === 'NEEDS_REVIEW').
    await expect(page.getByText(/disagrees with your saved records/i)).toBeVisible();
    const save = page.getByRole('button', { name: 'Save decisions & continue' });
    await expect(save).toBeVisible();
    // Disabled until every contradiction is decided (`disabled={!allChosen}`).
    await expect(save).toBeDisabled();

    // --- 4. Reject every contradiction (keep our stored value) and save. ---
    const rejects = page.locator('input[name^="decision-"][value="REJECT"]');
    const count = await rejects.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      await rejects.nth(i).check();
    }
    await expect(save).toBeEnabled();
    await save.click();

    // --- 5. Batch flips to VALIDATED -> Step 3 Generate re-enables -> COMPLETED. ---
    const generate = page.getByRole('button', { name: /Generate \d+ challans?/ });
    await expect(generate).toBeVisible({ timeout: 15_000 });
    await generate.click();
    await expect(page.getByRole('button', { name: 'Download ZIP' })).toBeEnabled({
      timeout: 20_000,
    });
    await expect(page.getByText(/Done — \d+ challans? issued/)).toBeVisible();
  });
});
