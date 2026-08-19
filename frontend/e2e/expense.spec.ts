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
    // --- upload + real extraction ---
    await page.goto(EXPENSE);
    await page.setInputFiles('#expense-files', INVOICE);
    await page.getByRole('button', { name: /Upload \d+ file/ }).click();

    // The clean fixture extracts fully → an EXTRACTED outcome with a "View" action.
    await expect(page.getByText('Extracted').first()).toBeVisible({ timeout: 20_000 });
    await page.getByRole('link', { name: 'View' }).first().click();

    // --- the extracted canonical record + confirm ---
    const confirm = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirm).toBeEnabled({ timeout: 15_000 });
    await confirm.click();

    // Confirmed → the record is frozen (the Confirm action goes away / status flips).
    await expect(page.getByText('Confirmed').first()).toBeVisible({ timeout: 15_000 });
  });
});
