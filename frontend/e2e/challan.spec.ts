import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const TEMPLATE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'template.xlsx');

// The headline statutory flow, end-to-end through the real SPA + backend: upload the
// template (its example rows are valid against the e2e-bootstrapped master data) ->
// validate -> reserve+generate (stub PDF renderer) -> the register shows the issued
// challans -> void one. Proves the whole lifecycle without container-only WeasyPrint.
test.describe('Challan lifecycle', () => {
  test('upload -> validate -> generate -> register -> void', async ({ page }) => {
    await page.goto('/m/document_automation/new');

    // Upload + validate.
    await page.setInputFiles('#challan-file', TEMPLATE);
    await page.getByRole('button', { name: 'Upload & validate' }).click();

    // Validated -> pick the numbering series (a dropdown of configured series) then Generate.
    await page.getByLabel('Series').selectOption('L');
    const generate = page.getByRole('button', { name: /Generate 2 challans?/ });
    await expect(generate).toBeVisible();
    await generate.click();

    // Generation is async; the stub renderer makes it fast. Completion = the batch's
    // ZIP artifact becomes downloadable.
    await expect(page.getByRole('button', { name: 'Download ZIP' })).toBeEnabled({
      timeout: 20_000,
    });

    // The register shows the first issued challan.
    await page.goto('/m/document_automation/register');
    const firstNo = /GIF\/DC\/26-27\/L\/000001/;
    const issuedRow = page.getByRole('row', { name: firstNo });
    await expect(issuedRow).toBeVisible();
    await expect(issuedRow.getByText('ISSUED')).toBeVisible();

    // Void it (admin) with a reason.
    await issuedRow.getByRole('button', { name: 'Void' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Reason').fill('E2E void test');
    await dialog.getByRole('button', { name: 'Void challan' }).click();

    // Its status flips to VOID.
    await expect(page.getByRole('row', { name: firstNo }).getByText('VOID')).toBeVisible();

    // Filter the register by status, and export it as CSV (a real backend request).
    await page.getByLabel('Status').selectOption('VOID');
    await expect(page.getByRole('row', { name: firstNo })).toBeVisible();
    const [csv] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/challan/challans.csv') && r.status() === 200,
      ),
      page.getByRole('button', { name: 'Download CSV' }).click(),
    ]);
    expect(csv.ok()).toBeTruthy();
  });
});
