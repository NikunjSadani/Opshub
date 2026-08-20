import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// Expense cost-allocation (inc 27), end-to-end through the REAL SPA + backend.
//
// Every expense upload must be tagged with a required Project (active) + Payment
// method (admin-managed, Manage-gated). This spec drives that whole loop as the
// default Administrator (who holds MANAGE + expense.delete):
//   1. create an active Project (register a client, then a project under it),
//   2. add a Payment method on the Manage-gated Payment Methods tab,
//   3. Upload — the Upload button stays DISABLED until BOTH tags + a file are
//      chosen, then the real invoice PDF extracts cleanly,
//   4. the Register row carries the chosen Project code + Payment method,
//   5. confirm the invoice, then the Overview dashboard attributes the confirmed
//      spend to that Project + Payment method.
//
// Reuses the SAME gold invoice fixture as expense.spec.ts. Because the harness
// shares one sqlite DB across the whole run and dedup is by invoice identity AND
// source-byte hash, this spec DELETES its uploaded invoice in afterEach (via the
// real API as dev-admin) so the later expense.spec upload of the same fixture is
// still a fresh EXTRACTED — not a DUPLICATE.
const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');
const EXPENSE = '/m/expense_invoice';

// A client code distinct from the harness's other specs (bootstrap "BRI",
// projects.spec "ZZZ") so the created project never collides.
const CLIENT_CODE = 'ALC';
const PROJECT_CODE = `${CLIENT_CODE}-001`; // first project under a fresh client
const METHOD = 'Bank Transfer';

test.describe('Expense / cost-allocation', () => {
  // The invoice id created by this spec, so afterEach can clean it up regardless
  // of where an assertion might fail.
  let createdInvoiceId: string | null = null;

  test.afterEach(async ({ request }) => {
    if (!createdInvoiceId) return;
    // Delete as the Administrator (dev-admin holds expense.delete, needed once the
    // invoice is CONFIRMED). Best-effort: the DB is torn down at run end anyway.
    await request
      .delete(`/api/v1/expense/invoices/${createdInvoiceId}`, {
        // The dev-auth shim requires a bearer token AS WELL AS the acting-uid header;
        // without Authorization the delete 401s and the cleanup silently no-ops,
        // leaving a DUPLICATE that breaks the later same-fixture upload in expense.spec.
        headers: { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' },
      })
      .catch(() => {});
    createdInvoiceId = null;
  });

  test('project + payment method gate upload; register + overview attribute the spend', async ({
    page,
  }) => {
    // ---- 1. an ACTIVE project (register a client, then a project under it) ----
    await page.goto('/m/projects/clients');
    await page.getByRole('button', { name: 'New client' }).click();
    const clientDialog = page.getByRole('dialog', { name: 'Register client' });
    await clientDialog.getByLabel('Name').fill('Allocation Client');
    await clientDialog.getByLabel('Code').fill(CLIENT_CODE);
    await clientDialog.getByRole('button', { name: 'Register' }).click();
    await expect(page.getByRole('cell', { name: CLIENT_CODE, exact: true })).toBeVisible();

    await page.goto('/m/projects');
    await page.getByRole('button', { name: 'New project' }).click();
    const projectDialog = page.getByRole('dialog', { name: 'New project' });
    const clientValue = await projectDialog
      .locator('option', { hasText: CLIENT_CODE })
      .getAttribute('value');
    await projectDialog.getByLabel('Client').selectOption(clientValue!);
    await projectDialog.getByLabel('Name').fill('Allocation Project');
    await projectDialog.getByRole('button', { name: 'Create project' }).click();
    await expect(page.getByRole('cell', { name: PROJECT_CODE })).toBeVisible();

    // ---- 2. add a Payment method on the Manage-gated Payment Methods tab ----
    await page.goto(`${EXPENSE}/payment-methods`);
    await expect(page.getByRole('heading', { name: 'Payment methods' })).toBeVisible();
    await page.getByRole('button', { name: 'Add payment method' }).click();
    const methodDialog = page.getByRole('dialog', { name: 'Add payment method' });
    await methodDialog.getByLabel('Name').fill(METHOD);
    await methodDialog.getByRole('button', { name: 'Create' }).click();
    // The new method appears in the list as an Active row.
    const methodRow = page.getByRole('row').filter({ hasText: METHOD });
    await expect(methodRow).toBeVisible();
    await expect(methodRow.getByText('Active')).toBeVisible();

    // ---- 3. Upload: disabled until BOTH tags + a file are chosen ----
    await page.goto(EXPENSE);
    const uploadBtn = page.getByRole('button', { name: /^Upload/ });
    const projectSelect = page.getByLabel('Project');
    const methodSelect = page.getByLabel('Payment method');

    // Nothing chosen yet -> disabled.
    await expect(uploadBtn).toBeDisabled();

    // A file, but neither tag -> still disabled.
    await page.setInputFiles('#expense-files', INVOICE);
    await expect(uploadBtn).toBeDisabled();

    // Only the project chosen -> still disabled (payment method still required).
    const projectOption = await projectSelect
      .locator('option', { hasText: PROJECT_CODE })
      .getAttribute('value');
    await projectSelect.selectOption(projectOption!);
    await expect(uploadBtn).toBeDisabled();

    // Both tags now chosen -> enabled.
    await methodSelect.selectOption({ label: METHOD });
    await expect(uploadBtn).toBeEnabled();

    // Upload -> the clean fixture extracts fully (real pdfplumber extractor).
    await uploadBtn.click();
    await expect(page.getByText('Extracted').first()).toBeVisible({ timeout: 20_000 });

    // ---- 4. Register: the row carries the chosen Project + Payment method ----
    await page.getByRole('link', { name: 'Register', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Register' })).toBeVisible();
    // A single invoice row that shows BOTH tags together (same-row assertion).
    const invRow = page
      .getByRole('row')
      .filter({ hasText: PROJECT_CODE })
      .filter({ hasText: METHOD });
    await expect(invRow).toHaveCount(1);

    // Capture the invoice id from its View link, for confirm + afterEach cleanup.
    const href = await invRow.getByRole('link', { name: 'View' }).getAttribute('href');
    const idMatch = href?.match(/\/invoices\/(\d+)/);
    expect(idMatch, `expected an invoice id in the View href, got: ${href}`).not.toBeNull();
    createdInvoiceId = idMatch![1];

    // ---- 5. Confirm, then Overview attributes the confirmed spend ----
    await invRow.getByRole('link', { name: 'View' }).click();
    const confirm = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirm).toBeEnabled({ timeout: 15_000 });
    await confirm.click();
    await expect(page.getByText('Confirmed').first()).toBeVisible({ timeout: 15_000 });

    await page.getByRole('link', { name: 'Overview', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    // The confirmed-spend dashboard renders its total tile...
    await expect(page.getByText('Confirmed spend')).toBeVisible();
    // ...and the by-project / by-payment-method breakdowns (built only from
    // CONFIRMED invoices) attribute this spend to our project + method.
    await expect(
      page.getByRole('cell', { name: PROJECT_CODE, exact: true }),
    ).toBeVisible();
    await expect(page.getByRole('cell', { name: METHOD, exact: true })).toBeVisible();
  });
});
