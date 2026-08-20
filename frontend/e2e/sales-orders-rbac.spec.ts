import { test, expect, type APIRequestContext } from '@playwright/test';

/**
 * Sales Orders — RBAC gating, end-to-end through the REAL SPA + backend.
 *
 * The dev switcher ("Act as (dev)") picks which SEEDED user we act as; that sets an
 * `X-Dev-Uid` header so `GET /me` returns that user's REAL permissions and the UI
 * re-gates live. We switch IN PLACE (never page.goto/reload — a reload remounts the
 * mock provider back to `dev-admin`) and navigate by CLICKING nav links, so every
 * assertion is against the actually-served permission set. The backend 403s are the
 * real gate; this proves the write affordances honestly track the level:
 *   VIEW    — read the register + detail, NO create / edit / destructive actions.
 *   OPERATE — create a PO, but NO MANAGE-only Void / Short-close.
 *   MANAGE  — the lot (covered by the admin flow in sales-orders.spec).
 *
 * The seeded "Challan Operator" preset carries no sales_orders grant, so to exercise
 * the OPERATE tier we (as dev-admin, via the real API) mint a role WITH sales_orders
 * OPERATE and reassign the dev-operator user to it — seeding via the API rather than
 * editing the backend seed. It keeps document_automation OPERATE too, so nothing the
 * earlier challan RBAC spec relied on regresses.
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

const CLIENT_CODE = 'RBC';
const PROJECT_CODE = `${CLIENT_CODE}-001`;
const CLIENT_NAME = 'RBAC Client';
const PO_NUMBER = 'PO-RBAC-1';
const PRODUCT = 'Rbacish Gadget';

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

test.describe('Sales Orders — RBAC gating', () => {
  test('viewer sees no write affordances; operator has New PO but no Void/Short-close', async ({
    page,
    request,
  }) => {
    // ---- seed (as dev-admin): master data + a non-terminal DRAFT PO to open ----
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT_NAME, code: CLIENT_CODE });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'RBAC Project' });
    await apiPost(request, '/purchase-orders', {
      po_number: PO_NUMBER,
      client_id: client.id,
      project_id: project.id,
      po_date: new Date().toISOString().slice(0, 10),
      lines: [
        { product_id: product.id, ordered_qty: '5', cost_price_paise: 10000, sell_price_paise: 15000 },
      ],
    });

    // Grant the dev-operator user sales_orders OPERATE via a purpose-made role, so the
    // OPERATE tier is exercisable through the (fixed) dev switcher.
    const users = await (await request.get('/api/v1/users', { headers: DEV_ADMIN })).json();
    const operator = users.find((u: { email: string }) => u.email === 'operator@opshub.local');
    expect(operator, 'seeded operator@opshub.local must exist').toBeTruthy();
    const role = await apiPost(request, '/roles', {
      name: 'Sales Ops (E2E)',
      description: 'E2E: sales_orders OPERATE for the RBAC spec.',
      module_levels: { sales_orders: 'OPERATE', document_automation: 'OPERATE' },
      platform: [],
    });
    const patch = await request.patch(`/api/v1/users/${operator.id}`, {
      headers: DEV_ADMIN,
      data: { role_id: role.id },
    });
    expect(patch.ok(), `role reassign -> ${patch.status()} ${await patch.text()}`).toBeTruthy();

    const switcher = page.getByLabel('Dev user switcher');
    const sidebar = page.getByRole('navigation');
    const salesLink = sidebar.getByRole('link', { name: 'Purchase Orders' });

    await page.goto('/');

    // ================= VIEWER (VIEW only) =================
    await switcher.selectOption({ label: 'Viewer' });

    await salesLink.click();
    await expect(page.getByRole('heading', { name: 'Purchase orders' })).toBeVisible();
    // No OPERATE affordances on the register.
    await expect(page.getByRole('button', { name: 'New PO' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Upload .xlsx' })).toHaveCount(0);

    // Products tab — no MANAGE "New product".
    await page.getByRole('link', { name: 'Products' }).click();
    await expect(page.getByRole('heading', { name: 'Products' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'New product' })).toHaveCount(0);

    // PO detail — no MANAGE Void / Short-close (nor OPERATE Amend) for a viewer.
    await salesLink.click();
    await page.getByRole('row').filter({ hasText: PO_NUMBER }).getByRole('link', { name: 'View' }).click();
    await expect(page.getByRole('heading', { name: `PO ${PO_NUMBER}` })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Void' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Short-close' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Amend' })).toHaveCount(0);

    // ================= OPERATOR (OPERATE, no MANAGE) =================
    await switcher.selectOption({ label: 'Challan Operator' });

    // New PO (OPERATE) is now present on the register.
    await salesLink.click();
    await expect(page.getByRole('heading', { name: 'Purchase orders' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'New PO' })).toBeVisible();

    // But the PO detail still withholds the MANAGE-only actions (Void / Short-close),
    // while OPERATE Amend is available.
    await page.getByRole('row').filter({ hasText: PO_NUMBER }).getByRole('link', { name: 'View' }).click();
    await expect(page.getByRole('heading', { name: `PO ${PO_NUMBER}` })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Amend' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Void' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Short-close' })).toHaveCount(0);
  });
});
