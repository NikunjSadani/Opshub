import { test, expect, type Locator, type APIRequestContext } from '@playwright/test';

/**
 * Project Spine — Wave 4, the Action Center dashboard driven end-to-end through the
 * REAL SPA + FastAPI backend as the default Administrator (VIEW everywhere).
 *
 * The Action Center (`/m/action_center`, API `GET /action-center?horizon_days=15`) is a
 * READ-ONLY "what needs attention" rollup computed on read from Project-Spine data — three
 * count tiles over three sections:
 *   • Procurement follow-ups — POs whose `expected_procurement_date` is within the 15-day
 *     horizon and still live (status not CLOSED/CANCELLED).
 *   • Invoicing due          — POs (not CANCELLED) with Σ open-to-invoice qty > 0.
 *   • Overdue receivables    — CONFIRMED client invoices past their due date.
 *
 * This spec seeds — via the REAL API as dev-admin (the dev-auth shim needs BOTH a bearer
 * token AND the acting-uid header) — a client + project + a single PO that lands in BOTH
 * the procurement AND the invoicing-due categories at once:
 *   - `expected_procurement_date` ~10 days out  → within the 15-day horizon → Procurement,
 *   - one priced line, never invoiced (open qty 10 > 0) → Invoicing due.
 * Then, purely through the UI:
 *   1. load /m/action_center; the Procurement section lists the PO with its "in ~N days"
 *      cell; the Invoicing-due section lists the same PO with its ₹ uninvoiced value;
 *   2. both category count tiles read ≥ 1;
 *   3. the procurement row's PO link deep-links to /m/sales_orders/{po_id} — click it and
 *      land on the PO detail (heading "PO <number>");
 *   4. the invoicing-due row's PO link deep-links the same way.
 *
 * The AR-overdue section needs a CONFIRMED invoice with a PAST due date, which cannot be
 * seeded cleanly here (the billing capture path's fixture carries no due date), so this
 * spec asserts only that the "Overdue receivables" section RENDERS — it does not assert
 * its content (per the task's explicit fallback).
 *
 * afterEach voids the seeded PO (→ CANCELLED) so it drops out of every Action Center
 * category, leaving no residue for later specs in the shared sqlite DB.
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// Distinct 3-letter client code (bootstrap BRI, projects ZZZ, expense EXC,
// expense-allocation ALC, sales-orders SPN, sales-orders-rbac RBC, billing BLG,
// billing-rbac BRB, credit-notes CNR, credit-notes-rbac CNT, GEN overhead) so this
// spec never collides in the shared DB.
const CLIENT_CODE = 'ACT';
const CLIENT_NAME = 'Action Center Client';
const PROJECT_CODE = `${CLIENT_CODE}-001`;
const PO_NUMBER = 'PO-ACT-1';
const PRODUCT = 'Actionable Widget';

// The PO's expected-procurement date is this many days out — inside the 15-day horizon,
// so it appears under Procurement with a "in 10 days" cell.
const DAYS_OUT = 10;

// One line: qty 10 @ ₹150 sell → uninvoiced value = 10 × ₹150 = ₹1,500.00.
const UNINVOICED_VALUE = '1,500.00';

/** POST JSON to the real API as dev-admin, asserting a 2xx, returning the parsed body. */
async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

/** YYYY-MM-DD for `offset` days from today (relative — never a hardcoded date that rots). */
function isoInDays(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

test.describe('Action Center — Wave 4 (admin end-to-end)', () => {
  let poId: number | null = null;

  test.afterEach(async ({ request }) => {
    // Void the seeded PO so it leaves every Action Center category; idempotent + tolerant
    // of an already-cancelled / never-created PO.
    if (poId != null) {
      await request.post(`/api/v1/purchase-orders/${poId}/void`, {
        headers: DEV_ADMIN,
        data: { reason: 'E2E cleanup — action-center spec' },
      });
      poId = null;
    }
  });

  test('procurement + invoicing-due surface the PO and deep-link to its detail', async ({
    page,
    request,
  }) => {
    // ---- seed master data + the dual-category PO via the real API (as dev-admin) ----
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', {
      name: CLIENT_NAME,
      code: CLIENT_CODE,
    });
    const project = await apiPost(request, '/projects', {
      client_id: client.id,
      name: 'Action Center Project',
    });
    expect(project.code).toBe(PROJECT_CODE);

    const po = await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: isoInDays(0),
      // ~10 days out → inside the 15-day procurement horizon.
      expected_procurement_date: isoInDays(DAYS_OUT),
      lines: [
        // Priced + never invoiced → open-to-invoice qty 10 > 0 → Invoicing due.
        { product_id: product.id, ordered_qty: '10', cost_price_paise: 10000, client_sell_price_paise: 15000 },
      ],
    });
    poId = po.id;
    expect(po.po_number).toBe(PO_NUMBER);

    // Confirm the PO (DRAFT -> CONFIRMED): invoicing-due is CONFIRMED/IN_PROGRESS-only now.
    const confirmed = await apiPost(request, `/purchase-orders/${poId}/confirm`, {});
    expect(confirmed.status).toBe('CONFIRMED');

    // ================= load the Action Center =================
    await page.goto('/m/action_center');
    await expect(page.getByRole('heading', { name: 'Action Center' })).toBeVisible();

    // Scope to each section by its <h2> so we never confuse the two PO rows.
    const procSection = page.locator('section', {
      has: page.getByRole('heading', { name: 'Procurement follow-ups' }),
    });
    const invSection = page.locator('section', {
      has: page.getByRole('heading', { name: 'Invoicing due' }),
    });

    // ---- 1a. Procurement section lists the PO with its "in ~N days" cell ----
    const procRow = procSection.getByRole('row').filter({ hasText: PO_NUMBER });
    await expect(procRow).toHaveCount(1);
    await expect(procRow).toContainText(CLIENT_NAME);
    await expect(procRow).toContainText(PROJECT_CODE);
    // The relative "When" cell — resilient to the exact day count.
    await expect(procRow).toContainText(new RegExp(`in \\d+ days?`));

    // ---- 1b. Invoicing-due section lists the same PO with its ₹ uninvoiced value ----
    const invRow = invSection.getByRole('row').filter({ hasText: PO_NUMBER });
    await expect(invRow).toHaveCount(1);
    await expect(invRow).toContainText(CLIENT_NAME);
    await expect(invRow).toContainText(UNINVOICED_VALUE);

    // ---- 2. both category count tiles read ≥ 1 ----
    // Each tile's value <p> is the sibling BEFORE its unique hint text — this disambiguates
    // the tile label from the section <h2> of the same words.
    const procCount = page.getByText('Due within 15 days').locator('xpath=preceding-sibling::p[1]');
    await expect(procCount).toBeVisible();
    await expect(procCount).not.toHaveText('0');

    const invCount = page
      .getByText('Ordered but not yet invoiced')
      .locator('xpath=preceding-sibling::p[1]');
    await expect(invCount).toBeVisible();
    await expect(invCount).not.toHaveText('0');

    // ---- the third section renders (content not asserted — see file header) ----
    await expect(page.getByRole('heading', { name: 'Overdue receivables' })).toBeVisible();

    // ================= 3. invoicing-due row deep-links to the PO detail =================
    // (Assert the invoicing link first, then re-load and assert the procurement link, since
    //  clicking navigates away from the dashboard.)
    await invRow.getByRole('link', { name: PO_NUMBER }).click();
    await expect(page).toHaveURL(new RegExp(`/m/sales_orders/${poId}(\\b|$)`));
    await expect(page.getByRole('heading', { name: `PO ${PO_NUMBER}` })).toBeVisible();

    // ================= 4. procurement row deep-links to the PO detail =================
    await page.goto('/m/action_center');
    await expect(page.getByRole('heading', { name: 'Action Center' })).toBeVisible();
    const procRow2: Locator = page
      .locator('section', { has: page.getByRole('heading', { name: 'Procurement follow-ups' }) })
      .getByRole('row')
      .filter({ hasText: PO_NUMBER });
    await procRow2.getByRole('link', { name: PO_NUMBER }).click();
    await expect(page).toHaveURL(new RegExp(`/m/sales_orders/${poId}(\\b|$)`));
    await expect(page.getByRole('heading', { name: `PO ${PO_NUMBER}` })).toBeVisible();
  });
});
