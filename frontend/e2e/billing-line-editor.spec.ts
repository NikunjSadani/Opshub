import { test, expect, type Page, type Locator, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Billing — the MANUAL LINE EDITOR (add / edit / delete line items) driven END-TO-END
 * through the real SPA + FastAPI backend as the default Administrator.
 *
 * The whole point of the feature is rescuing an invoice the extractor left LINELESS: an
 * EXTRACTED (clean header, zero lines) invoice can't be confirmed (confirm needs ≥1 line)
 * and, before this feature, had no way to gain one. This spec proves the rescue at runtime:
 *
 *   1. upload the shared GST fixture PO-LESS (a standalone AR record; its one clean line
 *      makes it MATCHED — a PO-less invoice needs no line↔PO matching),
 *   2. DELETE that line through the UI -> the invoice goes LINELESS / EXTRACTED,
 *   3. ADD a line through the modal editor -> it reappears and the status recovers,
 *   4. EDIT the line's description -> the change persists,
 *   5. CONFIRM -> CONFIRMED (the rescue is complete).
 *
 * Money integrity: editing lines never touches grand_total_paise (the receivable), so this
 * spec confirms a PO-less standalone AR record whose header total is the fixture's.
 *
 * The invoice fixture's identity is GLOBALLY deduped (client GSTIN | number | date | total)
 * and the raw bytes are content-hashed, so an afterEach deletes this spec's invoice to free
 * the fixture for the other billing specs (mirrors billing-credit-notes' cleanup).
 */

const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');

// The dev-auth shim requires BOTH a bearer token AND the acting-uid header.
const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// A distinct 3-letter client code (bootstrap BRI, projects ZZZ, expense EXC,
// expense-allocation ALC, sales-orders SPN, sales-orders-rbac RBC, GEN overhead,
// billing BLG, billing-rbac BRB, credit-notes CNT) so this spec never collides.
const CLIENT_CODE = 'LNE';
const CLIENT_NAME = 'Line Editor Client';

// The fixture's extracted identity + its single extracted line.
const INVOICE_NUMBER = 'UMG/2026/0042';
const GRAND_TOTAL = '19,470.00';
const EXTRACTED_LINE = 'Steel almirah'; // the fixture's one priced line

/** POST JSON to the real API as dev-admin, asserting a 2xx, returning the parsed body. */
async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

/** Pick from an entity SearchableSelect combobox: open, filter, click the first option. */
async function pickCombo(root: Page | Locator, nameRe: RegExp, filterText: string): Promise<void> {
  const combo = root.getByRole('combobox', { name: nameRe });
  await combo.click();
  await combo.fill(filterText);
  await root.getByRole('listbox').getByRole('option').first().click();
}

test.describe('Billing — manual line editor (admin end-to-end)', () => {
  let clientId: string | null = null;

  // Free the globally-deduped fixture for the other billing specs: delete every invoice
  // (and so the confirmed AR record) this spec created for its client.
  test.afterEach(async ({ request }) => {
    if (clientId == null) return;
    const invoices = await request
      .get(`/api/v1/billing/invoices?client_id=${clientId}`, { headers: DEV_ADMIN })
      .then((r) => (r.ok() ? r.json() : []))
      .catch(() => []);
    for (const inv of invoices) {
      await request
        .delete(`/api/v1/billing/invoices/${inv.id}`, { headers: DEV_ADMIN })
        .catch(() => {});
    }
    clientId = null;
  });

  test('rescue a lineless invoice: delete the line -> EXTRACTED -> add + edit -> confirm', async ({
    page,
    request,
  }) => {
    // ---- seed a client (PO-less: no PO/project needed) ----
    const client = await apiPost(request, '/projects/clients', {
      name: CLIENT_NAME,
      code: CLIENT_CODE,
    });
    clientId = String(client.id);

    // ================= 1. upload PO-less + real extraction =================
    await page.goto('/m/billing/upload');
    await expect(page.getByRole('heading', { name: 'Upload client invoices' })).toBeVisible();
    // Pick the client only — leave the PO blank so this is a standalone (PO-less) AR record.
    await pickCombo(page, /^Client/, CLIENT_NAME);
    await page.setInputFiles('#billing-files', INVOICE);
    await page.getByRole('button', { name: /^Upload \d+ file/ }).click();

    // A PO-less invoice with a clean single line + clean required fields lands MATCHED.
    await page.getByRole('link', { name: 'Invoices', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Client invoices' })).toBeVisible();
    const regRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(regRow).toHaveCount(1);
    await expect(regRow).toContainText(GRAND_TOTAL);
    await regRow.getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();

    // The fixture's one extracted line is present.
    await expect(page.getByText(EXTRACTED_LINE).first()).toBeVisible();

    // ================= 2. DELETE the line -> lineless / EXTRACTED =================
    await page.getByRole('button', { name: 'Delete line 1' }).click();
    const rmDialog = page.getByRole('dialog', { name: 'Delete this line?' });
    await rmDialog.getByRole('button', { name: 'Delete line' }).click();
    await expect(rmDialog).toBeHidden();
    // The invoice is now lineless: the empty-state shows and the status badge reads Extracted.
    await expect(page.getByText('No line items').first()).toBeVisible();
    await expect(page.getByText(EXTRACTED_LINE)).toHaveCount(0);
    // Confirm is blocked while lineless.
    await expect(page.getByRole('button', { name: 'Confirm invoice' })).toBeDisabled();

    // ================= 3. ADD a line through the modal editor =================
    await page.getByRole('button', { name: 'Add line' }).click();
    const addModal = page.getByRole('dialog', { name: 'Add line item' });
    await addModal.getByLabel('Description').fill('Rescued line item');
    await addModal.getByLabel('Quantity').fill('3');
    await addModal.getByLabel('Taxable (₹)').fill('16500');
    await addModal.getByLabel('GST rate (%)').fill('18');
    await addModal.getByLabel('Line total (₹)').fill('19470');
    await addModal.getByRole('button', { name: 'Add line' }).click();
    await expect(addModal).toBeHidden();
    // The line reappears; confirm unlocks (PO-less -> MATCHED once it has a line).
    await expect(page.getByText('Rescued line item').first()).toBeVisible();

    // ================= 4. EDIT the line's description =================
    await page.getByRole('button', { name: /^Edit line/ }).first().click();
    const editModal = page.getByRole('dialog', { name: /^Edit line/ });
    await editModal.getByLabel('Description').fill('Rescued line item (edited)');
    await editModal.getByRole('button', { name: 'Save line' }).click();
    await expect(editModal).toBeHidden();
    await expect(page.getByText('Rescued line item (edited)').first()).toBeVisible();

    // ================= 5. CONFIRM — the rescue is complete =================
    const confirm = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirm).toBeEnabled({ timeout: 15_000 });
    await confirm.click();
    await expect(
      page.getByText('This invoice is confirmed. Its values are frozen.'),
    ).toBeVisible({ timeout: 15_000 });
  });
});
