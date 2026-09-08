import { test, expect, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';

/**
 * Client credit-note capture — RBAC gating, end-to-end through the REAL SPA + backend.
 *
 * The dev switcher ("Act as (dev)") picks which SEEDED user we act as; that sets an
 * `X-Dev-Uid` header so `GET /me` returns that user's REAL permissions and the UI re-gates
 * live. We switch IN PLACE (never page.goto/reload — a reload remounts the mock provider
 * back to `dev-admin`) and navigate by CLICKING nav links, so every assertion is against
 * the actually-served permission set. The backend 403s are the real gate; this proves the
 * credit-note write affordances honestly disappear for a VIEW-only user:
 *   - the register is READABLE and lists the seeded credit note,
 *   - Upload is BLOCKED (button disabled + an Operate-access message),
 *   - the detail shows NO Confirm (nor an enabled line-match select) and NO Cancel/Delete.
 *
 * The invoice + credit note are seeded via the real API (as dev-admin) and DELETED in
 * afterEach: the whole run shares one sqlite DB and capture dedup is by document identity
 * AND source bytes, so leaving the fixture-invoice would turn later same-fixture uploads
 * (billing-rbac.spec / billing.spec) into DUPLICATEs. Deleting keeps those fresh. The
 * `billing-credit-*` filename also sorts this ahead of those specs. A CN is a receivable
 * child, so it is deleted BEFORE the invoice it references.
 */

const INVOICE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'expense-invoice.pdf');
const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// Distinct client code (registry in billing.spec). CNR = credit-note RBAC.
const CLIENT_CODE = 'CNR';
const CLIENT_NAME = 'Credit Note RBAC Client';
const PO_NUMBER = 'PO-CNR-1';
const INVOICE_NUMBER = 'UMG/2026/0042'; // the fixture's extracted invoice number (== cn number)

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

test.describe('Billing — credit-note RBAC gating', () => {
  let clientId: string | null = null;

  test.afterEach(async ({ request }) => {
    if (!clientId) return;
    // CN(s) first (a receivable child blocks the invoice delete), then invoice(s). Best-effort.
    const cns = await request
      .get(`/api/v1/billing/credit-notes?client_id=${clientId}`, { headers: DEV_ADMIN })
      .then((r) => (r.ok() ? r.json() : []))
      .catch(() => []);
    for (const cn of cns) {
      await request.delete(`/api/v1/billing/credit-notes/${cn.id}`, { headers: DEV_ADMIN }).catch(() => {});
    }
    const invoices = await request
      .get(`/api/v1/billing/invoices?client_id=${clientId}`, { headers: DEV_ADMIN })
      .then((r) => (r.ok() ? r.json() : []))
      .catch(() => []);
    for (const inv of invoices) {
      await request.delete(`/api/v1/billing/invoices/${inv.id}`, { headers: DEV_ADMIN }).catch(() => {});
    }
    clientId = null;
  });

  test('viewer sees the CN register but no Upload / Confirm / Cancel affordances', async ({
    page,
    request,
  }) => {
    // ---- seed (as dev-admin): client + PO + a captured invoice + a credit note against it ----
    const product = await apiPost(request, '/products', { name: 'CN RBAC Line Item', uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT_NAME, code: CLIENT_CODE });
    clientId = String(client.id);
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'CN RBAC Project' });
    const po = await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: new Date().toISOString().slice(0, 10),
      lines: [
        { product_id: product.id, ordered_qty: '3', cost_price_paise: 400000, client_sell_price_paise: 550000 },
      ],
    });

    // Capture the invoice (multipart `files`), then a credit note against it (multipart `file`
    // + `invoice_id`). Both left in-review — the viewer only needs a CN to exist in the register.
    const invUp = await request.post('/api/v1/billing/invoices', {
      headers: DEV_ADMIN,
      multipart: {
        files: { name: 'client-invoice.pdf', mimeType: 'application/pdf', buffer: readFileSync(INVOICE) },
        client_id: String(client.id),
        po_id: String(po.id),
      },
    });
    expect(invUp.ok(), `invoice upload -> ${invUp.status()} ${await invUp.text()}`).toBeTruthy();
    const invoiceId = String((await invUp.json()).outcomes[0].invoice_id);
    expect(invoiceId).not.toBe('null');

    // A credit note can only be uploaded against a CONFIRMED invoice, so confirm it first:
    // match its (single) extracted line to the PO line, then confirm.
    const invDetail = await (
      await request.get(`/api/v1/billing/invoices/${invoiceId}`, { headers: DEV_ADMIN })
    ).json();
    await request.patch(
      `/api/v1/billing/invoices/${invoiceId}/lines/${invDetail.lines[0].id}/match`,
      { headers: DEV_ADMIN, data: { po_line_item_id: po.lines[0].id } },
    );
    const conf = await request.patch(`/api/v1/billing/invoices/${invoiceId}/review`, {
      headers: DEV_ADMIN,
      data: { confirm: true },
    });
    expect(conf.ok(), `invoice confirm -> ${conf.status()} ${await conf.text()}`).toBeTruthy();

    const cnUp = await request.post('/api/v1/billing/credit-notes', {
      headers: DEV_ADMIN,
      multipart: {
        file: { name: 'credit-note.pdf', mimeType: 'application/pdf', buffer: readFileSync(INVOICE) },
        invoice_id: invoiceId,
      },
    });
    expect(cnUp.ok(), `cn upload -> ${cnUp.status()} ${await cnUp.text()}`).toBeTruthy();
    const cnId = String((await cnUp.json()).outcomes[0].cn_id);
    expect(cnId).not.toBe('null');

    // ---- drive the UI as the Viewer (switch in place; navigate by clicking) ----
    const switcher = page.getByLabel('Dev user switcher');
    const sidebar = page.getByRole('navigation');
    await page.goto('/');
    await switcher.selectOption({ label: 'Viewer' });

    // Credit-note register is READABLE and lists the seeded CN.
    await sidebar.getByRole('link', { name: 'Billing & AR' }).click();
    await page.getByRole('link', { name: 'Credit Notes' }).click();
    await expect(page.getByRole('heading', { name: 'Credit notes' })).toBeVisible();
    const cnRow = page.getByRole('row').filter({ hasText: INVOICE_NUMBER });
    await expect(cnRow).toHaveCount(1);

    // Upload is BLOCKED: the button is disabled and the Operate-access message shows.
    await page.getByRole('link', { name: 'Upload credit notes' }).click();
    await expect(page.getByRole('heading', { name: 'Upload credit notes' })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Upload$|^Upload \d+ file/ })).toBeDisabled();
    await expect(
      page.getByText('You need Operate access to this module to upload.'),
    ).toBeVisible();

    // CN detail: NO Confirm affordance, the line-match select is disabled, the view-only
    // banner is shown, and no MANAGE Cancel / Delete controls (an operator/admin sees those).
    await page.getByRole('link', { name: /Back to credit notes/ }).click();
    await page.getByRole('row').filter({ hasText: INVOICE_NUMBER }).getByRole('link', { name: 'Review' }).click();
    await expect(page.getByRole('heading', { name: `Credit note ${INVOICE_NUMBER}` })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Confirm credit note' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Cancel credit note' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Delete' })).toHaveCount(0);
    await expect(page.locator('select[aria-label^="Match line"]').first()).toBeDisabled();
    await expect(page.getByText('You have view-only access to this credit note')).toBeVisible();
  });
});
