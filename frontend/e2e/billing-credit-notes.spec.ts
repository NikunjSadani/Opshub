import { test, expect, type Locator, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';

/**
 * Client CREDIT-NOTE capture — Wave 2, driven END-TO-END through the REAL SPA + FastAPI
 * backend as the default Administrator (MANAGE everywhere). A confirmed credit note REDUCES
 * the amount receivable against the (previously confirmed) invoice it references, and drops
 * that project's net revenue in Finance.
 *
 * This reuses billing.spec's exact setup to get a CONFIRMED sales invoice — seed a
 * client + project + PO via the real API, upload the shared GST fixture through the real
 * capture path, manual-map the (non-identifying) PO line, then confirm — then credits it:
 *   1. upload the SAME fixture PDF as a credit note against that confirmed invoice,
 *   2. assert it lands in the CN register against that invoice,
 *   3. open the CN detail, manual-map its line to the referenced invoice's PO line, then
 *      Confirm (unlocks only because the referenced invoice is CONFIRMED + every line mapped),
 *   4. downstream: the invoice's AR outstanding drops by the full credit (stays UNPAID — cash-based) AND the
 *      project's Finance revenue nets to zero (invoice taxable − equal credit-note taxable).
 *
 * SHARED-DB ORDERING (why this file is named `billing-credit-notes`): every spec that
 * captures the gold fixture as an INVOICE collides on the global content-hash / identity
 * dedup, so a fixture-invoice must be DELETED before the next such spec runs. billing.spec
 * keeps its confirmed fixture-invoice (it has receivable records and cannot be deleted), so
 * this spec MUST run BEFORE it — the `billing-credit-*` filename sorts ahead of
 * `billing-rbac` / `billing`. The afterEach deletes the CN then the invoice (a CN is a
 * receivable child that would otherwise block the invoice delete), freeing the fixture for
 * the later billing specs — the same discipline billing-rbac.spec already follows.
 */

const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');

// The dev-auth shim requires BOTH a bearer token AND the acting-uid header.
const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// A distinct 3-letter client code (registry in billing.spec: BRI/ZZZ/EXC/ALC/SPN/RBC/GEN/
// BLG/BRB) so this spec never collides in the shared sqlite DB. CNT = credit-note test.
const CLIENT_CODE = 'CNT';
const CLIENT_NAME = 'Credit Note Client';
const PROJECT_CODE = `${CLIENT_CODE}-001`;
const PO_NUMBER = 'PO-CNT-1';

// The fixture's extracted identity (shared with billing.spec). The credit note is extracted
// from the SAME PDF, so its cn_number == this invoice number too.
const INVOICE_NUMBER = 'UMG/2026/0042';
const GRAND_TOTAL = '19,470.00'; // ₹19,470.00 grand total (== the full credit)
const TAXABLE = '16,500.00'; //     ₹16,500.00 net-of-GST taxable (revenue, and the credit)

/** POST JSON to the real API as dev-admin, asserting a 2xx, returning the parsed body. */
async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

/** Select an <option> by (substring) visible text on a native <select>, waiting for it. */
async function pickOption(select: Locator, optionText: string): Promise<void> {
  const opt = select.locator('option', { hasText: optionText }).first();
  await opt.waitFor({ state: 'attached' });
  await select.selectOption((await opt.getAttribute('value'))!);
}

/** Map the (single) unmatched line select to the first real PO line, if not already set. */
async function mapFirstLine(select: Locator): Promise<void> {
  await expect(select).toBeEnabled();
  if (!(await select.inputValue())) {
    const opt = select.locator('option[value]:not([value=""])').first();
    await opt.waitFor({ state: 'attached' });
    await select.selectOption((await opt.getAttribute('value'))!);
  }
}

test.describe('Billing — client credit-note capture (admin end-to-end)', () => {
  // The client this spec created; used by afterEach to purge its CN + invoice so the shared
  // fixture is free for the later billing specs (see the shared-DB ordering note above).
  let clientId: string | null = null;

  test.afterEach(async ({ request }) => {
    if (!clientId) return;
    // Delete the credit note(s) FIRST (a CN is a receivable child that blocks the invoice
    // delete), then the invoice(s). Best-effort — the DB is torn down at the end of the run.
    const cns = await request
      .get(`/api/v1/billing/credit-notes?client_id=${clientId}`, { headers: DEV_ADMIN })
      .then((r) => (r.ok() ? r.json() : []))
      .catch(() => []);
    for (const cn of cns) {
      await request
        .delete(`/api/v1/billing/credit-notes/${cn.id}`, { headers: DEV_ADMIN })
        .catch(() => {});
    }
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

  test('confirmed invoice -> credit it -> AR outstanding + Finance revenue drop', async ({
    page,
    request,
  }) => {
    // ============ seed master data + a CONFIRMED invoice (billing.spec's setup) ============
    const product = await apiPost(request, '/products', {
      // Non-identifying (unrelated to the fixture line) so the auto-matcher scores below
      // threshold and the MANUAL map is exercised on both the invoice and the credit note.
      // Distinct name (products dedup on identity globally) so it never collides billing.spec.
      name: 'CN Generic Line Item',
      uom: 'PCS',
    });
    const client = await apiPost(request, '/projects/clients', {
      name: CLIENT_NAME,
      code: CLIENT_CODE,
    });
    clientId = String(client.id);
    const project = await apiPost(request, '/projects', {
      client_id: client.id,
      name: 'Credit Note Project',
    });
    expect(project.code).toBe(PROJECT_CODE);
    await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: new Date().toISOString().slice(0, 10),
      lines: [
        { product_id: product.id, ordered_qty: '3', cost_price_paise: 400000, sell_price_paise: 550000 },
      ],
    });

    // ---- upload the invoice PDF through the real capture path, then match + confirm ----
    await page.goto('/m/billing/upload');
    await expect(page.getByRole('heading', { name: 'Upload client invoices' })).toBeVisible();
    const invClientSelect = page.locator('select', {
      has: page.locator('option', { hasText: CLIENT_NAME }),
    });
    await pickOption(invClientSelect, CLIENT_NAME);
    const poSelect = page.locator('select', {
      has: page.locator('option', { hasText: PO_NUMBER }),
    });
    await pickOption(poSelect, PO_NUMBER);
    await page.setInputFiles('#billing-files', INVOICE);
    await page.getByRole('button', { name: /^Upload \d+ file/ }).click();
    await expect(page.getByText('Needs match').first()).toBeVisible({ timeout: 20_000 });

    // Open the invoice detail, manual-map the line, confirm.
    await page.getByRole('link', { name: 'Invoices', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Client invoices' })).toBeVisible();
    const invRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(invRow).toHaveCount(1);
    await invRow.getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();
    await mapFirstLine(page.locator('select[aria-label^="Match line"]').first());
    const confirmInvoice = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirmInvoice).toBeEnabled({ timeout: 15_000 });
    await confirmInvoice.click();
    await expect(
      page.getByText('This invoice is confirmed. Its values are frozen.'),
    ).toBeVisible({ timeout: 15_000 });

    // ================= 1. upload the credit note against the confirmed invoice =================
    await page.goto('/m/billing/credit-notes/upload');
    await expect(page.getByRole('heading', { name: 'Upload credit notes' })).toBeVisible();

    // The "Credited invoice" select lists only CONFIRMED invoices; ours now shows up.
    const cnInvoiceSelect = page.locator('select', {
      has: page.locator('option', { hasText: INVOICE_NUMBER }),
    });
    await pickOption(cnInvoiceSelect, INVOICE_NUMBER);
    await page.setInputFiles('#cn-files', INVOICE);
    await page.getByRole('button', { name: /^Upload \d+ file/ }).click();

    // The results panel offers a Review link to the stored credit note (not REJECTED).
    await expect(
      page.getByRole('link', { name: 'Review' }).first(),
    ).toBeVisible({ timeout: 20_000 });

    // ================= 2. it lands in the CN register against that invoice =================
    await page.getByRole('link', { name: /Back to credit notes/ }).click();
    await expect(page.getByRole('heading', { name: 'Credit notes' })).toBeVisible();
    const cnRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(cnRow).toHaveCount(1);
    await expect(cnRow).toContainText(GRAND_TOTAL);

    // ================= 3. detail: manual-map the line, then Confirm =================
    await cnRow.getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Credit note ${INVOICE_NUMBER}` })).toBeVisible();

    // The referenced invoice is surfaced as CONFIRMED — the gate for confirming the CN.
    await expect(page.getByRole('heading', { name: 'Credited invoice' })).toBeVisible();
    await expect(page.getByText('CONFIRMED').first()).toBeVisible();

    // Confirm is withheld while the line is UNMATCHED (the auto-matcher can't map the
    // non-identifying PO line), then unlocks after the MANUAL map.
    const confirmCn = page.getByRole('button', { name: 'Confirm credit note' });
    await expect(confirmCn).toBeDisabled();
    await mapFirstLine(page.locator('select[aria-label^="Match line"]').first());
    await expect(confirmCn).toBeEnabled({ timeout: 15_000 });
    await confirmCn.click();
    await expect(
      page.getByText('This credit note is confirmed. Its values are frozen.'),
    ).toBeVisible({ timeout: 15_000 });

    // ================= 4a. downstream: the invoice's AR outstanding drops =================
    await page.getByRole('link', { name: 'Receivables' }).click();
    await expect(page.getByRole('heading', { name: 'Receivables' })).toBeVisible();
    await page
      .getByRole('row')
      .filter({ hasText: INVOICE_NUMBER })
      .getByRole('link', { name: 'View' })
      .click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();

    // The confirmed credit note (₹19,470 grand total) fully credits the ₹19,470 invoice:
    // Credited = ₹19,470.00, Outstanding = ₹0.00. Status reflects CASH (owner decision):
    // no payment was made, so a fully-CREDITED-but-unpaid invoice stays UNPAID (credits
    // reduce what's owed but are not "paid").
    const creditedStat = page.getByText('Credited').locator('xpath=following-sibling::p[1]');
    await expect(creditedStat).toHaveText(`₹${GRAND_TOTAL}`);
    const outstandingStat = page
      .getByText('Outstanding', { exact: true })
      .locator('xpath=following-sibling::p[1]');
    await expect(outstandingStat).toHaveText('₹0.00');
    const statusBadge = page.getByText('Status', { exact: true }).locator('xpath=following-sibling::div[1]');
    await expect(statusBadge).toContainText('Unpaid');

    // ================= 4b. downstream: Finance revenue nets to zero =================
    await page.goto('/m/finance');
    await expect(page.getByRole('heading', { name: 'Finance / P&L' })).toBeVisible();

    // The per-project P&L attributes ₹16,500 invoice taxable AND an equal ₹16,500 credit-note
    // reduction to this project (both reached via the invoice's PO), so its net revenue is
    // ₹0.00 — the reduction billing.spec would otherwise see as the full ₹16,500.
    const pnlRow = page.getByRole('row').filter({ hasText: PROJECT_CODE });
    await expect(pnlRow).toBeVisible();
    // Revenue is the 3rd column (Client · Project · Revenue · Cost · Margin · Margin %).
    await expect(pnlRow.getByRole('cell').nth(2)).toHaveText('₹0.00');
    await expect(pnlRow).not.toContainText(TAXABLE);
  });
});
