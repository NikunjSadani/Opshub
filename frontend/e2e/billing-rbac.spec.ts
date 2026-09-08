import { test, expect, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';

/**
 * Billing & Finance — RBAC gating, end-to-end through the REAL SPA + backend.
 *
 * The dev switcher ("Act as (dev)") picks which SEEDED user we act as; that sets an
 * `X-Dev-Uid` header so `GET /me` returns that user's REAL permissions and the UI
 * re-gates live. We switch IN PLACE (never page.goto/reload — a reload remounts the mock
 * provider back to `dev-admin`) and navigate by CLICKING nav links, so every assertion is
 * against the actually-served permission set. The backend 403s are the real gate; this
 * proves the money-write affordances honestly disappear for a VIEW-only user:
 *   - no Upload (upload action blocked with an Operate-access message),
 *   - no Confirm affordance (nor an enabled line-match select) on an in-review invoice,
 *   - no Record payment on a receivable,
 *   - no Record advance on the advances register,
 * while the registers + the Finance dashboard stay READABLE (Viewer holds billing +
 * finance VIEW in the fresh seed).
 *
 * The fixture invoice is seeded via the real API (as dev-admin) and DELETED in afterEach:
 * the whole run shares one sqlite DB and billing dedup is by invoice identity AND source
 * bytes, so leaving it would turn the later billing.spec upload of the same fixture into a
 * DUPLICATE. Deleting it keeps that upload a fresh capture.
 */

const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');
const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// Distinct client code (see billing.spec for the shared-DB code registry).
const CLIENT_CODE = 'BRB';
const CLIENT_NAME = 'Billing RBAC Client';
const PO_NUMBER = 'PO-BRB-1';
const INVOICE_NUMBER = 'UMG/2026/0042'; // the fixture's extracted invoice number

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

test.describe('Billing & Finance — RBAC gating', () => {
  let createdInvoiceId: string | null = null;

  test.afterEach(async ({ request }) => {
    if (!createdInvoiceId) return;
    // Delete as dev-admin (delete needs MANAGE) so the later same-fixture upload in
    // billing.spec is a fresh capture, not a DUPLICATE. Best-effort (DB is torn down anyway).
    await request
      .delete(`/api/v1/billing/invoices/${createdInvoiceId}`, { headers: DEV_ADMIN })
      .catch(() => {});
    createdInvoiceId = null;
  });

  test('viewer sees the registers + finance but no write affordances', async ({ page, request }) => {
    // ---- seed (as dev-admin): client + PO + an in-review captured invoice ----
    const product = await apiPost(request, '/products', { name: 'RBAC Line Item', uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT_NAME, code: CLIENT_CODE });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Billing RBAC Project' });
    const po = await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: new Date().toISOString().slice(0, 10),
      lines: [
        { product_id: product.id, ordered_qty: '3', cost_price_paise: 400000, client_sell_price_paise: 550000 },
      ],
    });

    // Upload the fixture through the real capture path (multipart, as dev-admin). Left
    // IN-REVIEW (line unmatched) so the viewer can see the confirm gate withheld.
    const uploadRes = await request.post('/api/v1/billing/invoices', {
      headers: DEV_ADMIN,
      multipart: {
        files: {
          name: 'client-invoice.pdf',
          mimeType: 'application/pdf',
          buffer: readFileSync(INVOICE),
        },
        client_id: String(client.id),
        po_id: String(po.id),
      },
    });
    expect(uploadRes.ok(), `upload -> ${uploadRes.status()} ${await uploadRes.text()}`).toBeTruthy();
    const upload = await uploadRes.json();
    createdInvoiceId = String(upload.outcomes[0].invoice_id);
    expect(createdInvoiceId).not.toBe('null');

    // ---- drive the UI as the Viewer (switch in place; navigate by clicking) ----
    const switcher = page.getByLabel('Dev user switcher');
    const sidebar = page.getByRole('navigation');
    await page.goto('/');
    await switcher.selectOption({ label: 'Viewer' });

    // Billing register is READABLE and lists the captured invoice.
    await sidebar.getByRole('link', { name: 'Billing & AR' }).click();
    await expect(page.getByRole('heading', { name: 'Client invoices' })).toBeVisible();
    const regRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(regRow).toHaveCount(1);

    // Upload is BLOCKED: the button is disabled and the Operate-access message shows.
    await page.getByRole('link', { name: 'Upload invoices' }).click();
    await expect(page.getByRole('heading', { name: 'Upload client invoices' })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Upload/ })).toBeDisabled();
    await expect(
      page.getByText('You need Operate access to this module to upload.'),
    ).toBeVisible();

    // Invoice detail: NO Confirm affordance, the line-match select is disabled, and the
    // view-only banner is shown (an operator would see Confirm + an enabled select here).
    await page.getByRole('link', { name: 'Invoices', exact: true }).click();
    await page.getByRole('row').filter({ hasText: INVOICE_NUMBER }).getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Invoice ${INVOICE_NUMBER}` })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Confirm invoice' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Save corrections' })).toHaveCount(0);
    await expect(page.locator('select[aria-label^="Match line"]').first()).toBeDisabled();
    await expect(
      page.getByText('You have view-only access to this invoice'),
    ).toBeVisible();

    // Receivables register is READABLE; no Record-payment affordance is exposed to a viewer.
    await page.getByRole('link', { name: 'Receivables' }).click();
    await expect(page.getByRole('heading', { name: 'Receivables' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Record payment' })).toHaveCount(0);

    // Advances register is READABLE; the Record-advance action is withheld.
    await page.getByRole('link', { name: 'Advances' }).click();
    await expect(page.getByRole('heading', { name: 'Advances' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Record advance' })).toHaveCount(0);

    // Finance / P&L dashboard is READABLE.
    await sidebar.getByRole('link', { name: 'Finance & P&L' }).click();
    await expect(page.getByRole('heading', { name: 'Finance / P&L' })).toBeVisible();
    await expect(page.getByText('Total revenue')).toBeVisible();
  });
});
