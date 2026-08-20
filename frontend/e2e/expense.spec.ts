import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// A real, machine-generated GST invoice PDF (a gold fixture) driven through the REAL
// text-layer extractor in the e2e backend.
const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');
const EXPENSE = '/m/expense_invoice';

/**
 * Expense module, end-to-end: upload a vendor GST invoice PDF -> the backend extracts it
 * with pdfplumber (header + line items + totals + arithmetic checks) -> the invoice lands
 * in the register -> open it -> confirm the extracted canonical record.
 */
test.describe('Expense / Invoice', () => {
  test('upload -> extract -> review -> confirm an invoice', async ({ page }) => {
    // Cost allocation (inc 27) is now REQUIRED at upload, so seed a project + a payment
    // method via the real API (dev-auth acts as dev-admin) and select them below.
    const H = { Authorization: 'Bearer e2e' };
    const client = await (await page.request.post('/api/v1/projects/clients', {
      headers: H, data: { name: 'Expense Co', code: 'EXC' },
    })).json();
    const project = await (await page.request.post('/api/v1/projects', {
      headers: H, data: { client_id: client.id, name: 'Expense Project' },
    })).json();
    const method = await (await page.request.post('/api/v1/expense/payment-methods', {
      headers: H, data: { name: 'Cash' },
    })).json();

    // --- upload + real extraction (with the required allocation) ---
    await page.goto(EXPENSE);
    await page.getByLabel('Project').selectOption(String(project.id));
    await page.getByLabel('Payment method').selectOption(String(method.id));
    await page.setInputFiles('#expense-files', INVOICE);
    await page.getByRole('button', { name: /Upload \d+ file/ }).click();

    // The clean fixture extracts fully → an EXTRACTED outcome.
    await expect(page.getByText('Extracted').first()).toBeVisible({ timeout: 20_000 });

    // --- open the invoice from the Register (the reliable detail path) + confirm ---
    await page.getByRole('link', { name: 'Register', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Register' })).toBeVisible();
    const invRow = page.getByRole('row').filter({ hasText: project.code });
    await invRow.getByRole('link', { name: 'View' }).click();

    const confirm = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirm).toBeEnabled({ timeout: 15_000 });
    await confirm.click();

    // Confirmed → the record is frozen (the Confirm action goes away / status flips).
    await expect(page.getByText('Confirmed').first()).toBeVisible({ timeout: 15_000 });
  });
});
