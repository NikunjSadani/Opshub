import { test, expect, type Locator, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Billing / Finance — Wave 2, the money flows driven END-TO-END through the REAL
 * SPA + FastAPI backend as the default Administrator (MANAGE everywhere).
 *
 * The gold GST-invoice fixture (shared with expense.spec) is the same layout a CLIENT
 * invoice uses, so it is uploaded through the real billing capture path and driven by
 * the real pdfplumber text-layer extractor. That fixture extracts a single priced line
 * (Steel almirah 6ft · qty 3 · ₹5,500) with a ₹16,500 taxable / ₹19,470 grand total and
 * all five required identity fields clean (buyer GSTIN, invoice no., date, taxable, grand
 * total), so it can be matched + confirmed without any field correction.
 *
 * The seeded PO line is intentionally NON-identifying (a generic product, different HSN)
 * so the auto-matcher leaves the line UNMATCHED — this spec then exercises the MANUAL map
 * (the operator fallback) through the UI select. The map step is written to be a no-op if
 * a future matcher change already matches the line, so it stays robust either way.
 *
 * The whole loop, purely through the UI (seed master-data via the real API):
 *   1. upload the invoice PDF (client + PO chosen) -> lands in the register as Needs-match,
 *   2. open the detail, manually map the line to the PO line, then Confirm -> Confirmed,
 *   3. Receivables: record a PART payment -> Part-paid + correct outstanding + aging shown,
 *   4. Advances: record a client advance, then apply it to the invoice -> outstanding drops,
 *   5. Finance: the consolidated + per-project P&L reflect the confirmed invoice's revenue.
 */

const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');

// The dev-auth shim requires BOTH a bearer token AND the acting-uid header.
const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// A distinct 3-letter client code (bootstrap BRI, projects ZZZ, expense EXC,
// expense-allocation ALC, sales-orders SPN, sales-orders-rbac RBC, GEN overhead,
// billing-rbac BRB) so this spec never collides in the shared sqlite DB.
const CLIENT_CODE = 'BLG';
const CLIENT_NAME = 'Billing Client';
const PROJECT_CODE = `${CLIENT_CODE}-001`;
const PO_NUMBER = 'PO-BLG-1';

// The fixture's extracted identity — asserted throughout so a row can't collide.
const INVOICE_NUMBER = 'UMG/2026/0042';
const GRAND_TOTAL = '19,470.00'; // ₹19,470.00 grand total
const TAXABLE = '16,500.00'; //     ₹16,500.00 net-of-GST taxable = finance revenue

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

test.describe('Billing & Finance — Wave 2 money flows (admin end-to-end)', () => {
  test('upload -> match -> confirm -> receivables -> advances -> finance', async ({
    page,
    request,
  }) => {
    // ---- seed master data via the real API (as dev-admin) ----
    const product = await apiPost(request, '/products', {
      // Deliberately non-identifying (no code, unrelated to the fixture line) so the
      // auto-matcher scores below threshold and the MANUAL map is exercised below.
      name: 'Generic Line Item',
      uom: 'PCS',
    });
    const client = await apiPost(request, '/projects/clients', {
      name: CLIENT_NAME,
      code: CLIENT_CODE,
    });
    const project = await apiPost(request, '/projects', {
      client_id: client.id,
      name: 'Billing Project',
    });
    expect(project.code).toBe(PROJECT_CODE);
    await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: new Date().toISOString().slice(0, 10),
      lines: [
        // qty 3 == the fixture line's qty, so confirming does not soft-flag over-billing.
        { product_id: product.id, ordered_qty: '3', cost_price_paise: 400000, sell_price_paise: 550000 },
      ],
    });

    // ================= 1. upload + real extraction =================
    await page.goto('/m/billing/upload');
    await expect(page.getByRole('heading', { name: 'Upload client invoices' })).toBeVisible();

    // Locate each <select> by an option only it carries (getByLabel('Client') also matches
    // the PO select, whose placeholder option reads "Choose a client first").
    const clientSelect = page.locator('select', {
      has: page.locator('option', { hasText: CLIENT_NAME }),
    });
    await pickOption(clientSelect, CLIENT_NAME);
    const poSelect = page.locator('select', {
      has: page.locator('option', { hasText: PO_NUMBER }),
    });
    await pickOption(poSelect, PO_NUMBER);
    await page.setInputFiles('#billing-files', INVOICE);
    await page.getByRole('button', { name: /^Upload \d+ file/ }).click();

    // The clean fixture extracts fully; with a PO linked but the line not yet matched the
    // outcome is Needs-match (its single line still needs a PO line).
    await expect(page.getByText('Needs match').first()).toBeVisible({ timeout: 20_000 });

    // ---- it lands in the register (the durable, reload-safe view) ----
    await page.getByRole('link', { name: 'Invoices', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Client invoices' })).toBeVisible();
    const regRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(regRow).toHaveCount(1);
    await expect(regRow).toContainText(CLIENT_NAME);
    await expect(regRow).toContainText(GRAND_TOTAL);
    await expect(regRow).toContainText(/Needs match|Extracted/);

    // ================= 2. detail: manual map + confirm =================
    await regRow.getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();

    // Manually map the (single) invoice line to the PO line via the UI select. Robust to
    // either state: if a future auto-matcher already mapped it, the select already carries
    // a value and this is a no-op.
    const matchSelect = page.locator('select[aria-label^="Match line"]').first();
    await expect(matchSelect).toBeEnabled();
    if (!(await matchSelect.inputValue())) {
      const opt = matchSelect.locator('option[value]:not([value=""])').first();
      await opt.waitFor({ state: 'attached' });
      await matchSelect.selectOption((await opt.getAttribute('value'))!);
    }

    // Every required field is clean + the line is now mapped -> Confirm unlocks.
    const confirm = page.getByRole('button', { name: 'Confirm invoice' });
    await expect(confirm).toBeEnabled({ timeout: 15_000 });
    await confirm.click();
    await expect(
      page.getByText('This invoice is confirmed. Its values are frozen.'),
    ).toBeVisible({ timeout: 15_000 });

    // ================= 3. Receivables: a PART payment =================
    await page.getByRole('link', { name: 'Receivables' }).click();
    await expect(page.getByRole('heading', { name: 'Receivables' })).toBeVisible();

    // The confirmed invoice is now a receivable: Unpaid, full outstanding.
    const arRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(arRow).toContainText('Unpaid');
    await expect(arRow).toContainText(GRAND_TOTAL);
    await arRow.getByRole('link', { name: 'View' }).click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();

    // Record a PART payment of ₹9,470 (leaves ₹10,000.00 outstanding).
    await page.getByRole('button', { name: 'Record payment' }).click();
    const payDialog = page.getByRole('dialog', { name: 'Record payment' });
    await payDialog.getByLabel('Amount (₹)').fill('9470');
    await payDialog.getByRole('button', { name: 'Record payment' }).click();
    await expect(payDialog).toBeHidden();

    // The detail reflects the part-payment: Part-paid + ₹10,000.00 outstanding.
    await expect(page.getByText('Part-paid')).toBeVisible();
    await expect(page.getByText('₹10,000.00')).toBeVisible();

    // Back on the register the row shows Part-paid + the right outstanding + an Aging column.
    // (The fixture carries no due date, so this receivable's aging bucket reads "—".)
    await page.getByRole('link', { name: /Back to receivables/ }).click();
    await expect(page.getByRole('heading', { name: 'Receivables' })).toBeVisible();
    const arRow2 = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(arRow2).toContainText('Part-paid');
    await expect(arRow2).toContainText('10,000.00');
    await expect(page.getByRole('columnheader', { name: 'Aging' })).toBeVisible();

    // ================= 4. Advances: record + apply =================
    await page.getByRole('link', { name: 'Advances' }).click();
    await expect(page.getByRole('heading', { name: 'Advances' })).toBeVisible();

    await page.getByRole('button', { name: 'Record advance' }).click();
    const advDialog = page.getByRole('dialog', { name: 'Record advance' });
    await pickOption(advDialog.getByLabel('Client'), CLIENT_CODE);
    await advDialog.getByLabel('Amount (₹)').fill('4000');
    await advDialog.getByLabel('Reference').fill('ADV-BLG');
    await advDialog.getByRole('button', { name: 'Record advance' }).click();
    await expect(advDialog).toBeHidden();

    const advRow = page.getByRole('row').filter({ hasText: 'ADV-BLG' });
    await expect(advRow).toContainText('4,000.00');
    await expect(advRow).toContainText('Available');

    // Apply the advance to the invoice from its Receivables detail -> outstanding drops.
    await page.getByRole('link', { name: 'Receivables' }).click();
    await page
      .getByRole('row')
      .filter({ hasText: INVOICE_NUMBER })
      .getByRole('link', { name: 'View' })
      .click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();

    await page.getByRole('button', { name: 'Apply advance' }).click();
    const applyDialog = page.getByRole('dialog', { name: 'Apply advance' });
    // The Amount field's hint text contains the word "Advance", so getByLabel('Advance')
    // is ambiguous — the dialog has exactly one <select> (the advance picker), so target it.
    await pickOption(applyDialog.locator('select'), 'ADV-BLG');
    await applyDialog.getByLabel('Amount (₹)').fill('4000');
    await applyDialog.getByRole('button', { name: 'Apply advance' }).click();
    await expect(applyDialog).toBeHidden();

    // ₹10,000 outstanding − ₹4,000 advance = ₹6,000.00; ₹4,000.00 shows as applied.
    await expect(page.getByText('₹6,000.00')).toBeVisible();
    const appliedStat = page.getByText('Advances applied').locator('xpath=following-sibling::p[1]');
    await expect(appliedStat).toHaveText('₹4,000.00');

    // ================= 5. Finance: revenue reflects the invoice =================
    await page.goto('/m/finance');
    await expect(page.getByRole('heading', { name: 'Finance / P&L' })).toBeVisible();

    // The consolidated Total-revenue tile is non-zero (the confirmed invoice's taxable).
    const totalRevenue = page
      .getByText('Total revenue')
      .locator('xpath=following-sibling::p[1]');
    await expect(totalRevenue).toBeVisible();
    await expect(totalRevenue).not.toHaveText('₹0.00');

    // The per-project P&L attributes that revenue (net-of-GST taxable = ₹16,500.00) to
    // this invoice's project (reached via the invoice's PO).
    const pnlRow = page.getByRole('row').filter({ hasText: PROJECT_CODE });
    await expect(pnlRow).toContainText(TAXABLE);
  });
});
