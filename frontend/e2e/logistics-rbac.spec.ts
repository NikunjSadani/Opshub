import { test, expect, type APIRequestContext } from '@playwright/test';

/**
 * Logistics — RBAC gating, end-to-end through the REAL SPA + backend.
 *
 * The dev switcher ("Act as (dev)") picks which SEEDED user we act as; that sets an
 * `X-Dev-Uid` header so `GET /me` returns that user's REAL permissions and the UI
 * re-gates live. We switch IN PLACE (never page.goto/reload — a reload remounts the
 * mock provider back to `dev-admin`) and navigate by CLICKING nav links, so every
 * assertion is against the actually-served permission set.
 *
 * The seeded "Viewer" preset holds every module at VIEW only (no OPERATE, no MANAGE).
 * Server-side RBAC is the real gate (the backend 403s); this proves the write
 * affordances are honestly withheld while the read surfaces stay fully usable:
 *   - the register hides New shipment (OPERATE) + Upload .xlsx (OPERATE),
 *   - the detail hides the status-update control + POD attach (OPERATE) and Delete
 *     (MANAGE), yet the register + detail remain readable.
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// A distinct free-text challan number so this seeded row never collides with the
// logistics.spec rows in the shared sqlite DB.
const CHALLAN = 'GIF/DC/E2E-LOGRBAC/700';
const PARTNER = 'DTDC';

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

test.describe('Logistics — RBAC gating', () => {
  let shipmentId: string | null = null;

  // Clean up the seeded shipment regardless of where an assertion fails (delete as
  // dev-admin — the browser may be switched to Viewer, but the API context carries
  // its own admin acting-uid header).
  test.afterEach(async ({ request }) => {
    if (!shipmentId) return;
    await request
      .delete(`/api/v1/logistics/shipments/${shipmentId}`, { headers: DEV_ADMIN })
      .catch(() => {});
    shipmentId = null;
  });

  test('viewer sees no write affordances; register + detail stay readable', async ({
    page,
    request,
  }) => {
    // ---- seed one shipment via the real API (as dev-admin) ----
    const shipment = await apiPost(request, '/logistics/shipments', {
      challan_number: CHALLAN,
      delivery_partner: PARTNER,
      status: 'DISPATCHED',
      consignee_name: 'Gamma Distributors',
    });
    shipmentId = String(shipment.id);

    const switcher = page.getByLabel('Dev user switcher');
    // Scope to the sidebar nav — on the home dashboard a module CARD also links to
    // /m/logistics (name "Logistics Operations Open →"), so an unscoped match is ambiguous.
    const logisticsLink = page.getByRole('navigation').getByRole('link', { name: 'Logistics' });

    await page.goto('/');

    // ================= Viewer (VIEW only) =================
    await switcher.selectOption({ label: 'Viewer' });

    // ---- register: readable, but no OPERATE create/upload affordances ----
    await logisticsLink.click();
    await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'New shipment' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Upload .xlsx' })).toHaveCount(0);

    // The seeded row is still fully readable (VIEW is granted).
    const row = page.getByRole('row').filter({ hasText: CHALLAN });
    await expect(row).toHaveCount(1);
    await expect(row).toContainText(PARTNER);
    await expect(row).toContainText('Dispatched');

    // ---- detail: readable, but no OPERATE status/POD nor MANAGE Delete ----
    await row.getByRole('link', { name: 'View' }).click();
    await expect(page.getByRole('heading', { name: `Shipment ${CHALLAN}` })).toBeVisible();

    // Read surfaces render: the status Badge + the consignee subtitle are visible.
    // exact — "Dispatched" also appears as the "Dispatched on" <dt>.
    await expect(page.getByText('Dispatched', { exact: true })).toBeVisible();
    // Rendered both as the header subtitle and the Consignee field — either proves the read.
    await expect(page.getByText('Gamma Distributors').first()).toBeVisible();

    // The OPERATE inline status-update control is absent (whole section is gated).
    await expect(page.getByRole('heading', { name: 'Update status' })).toHaveCount(0);
    await expect(page.getByLabel('Delivery status')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Update' })).toHaveCount(0);

    // The Proof-of-delivery section still shows (read), but the OPERATE attach input
    // is absent — no file input anywhere on the page for a viewer.
    await expect(page.getByRole('heading', { name: 'Proof of delivery' })).toBeVisible();
    await expect(page.locator('input[type="file"]')).toHaveCount(0);

    // The MANAGE-only Delete action is absent.
    await expect(page.getByRole('button', { name: 'Delete' })).toHaveCount(0);
  });
});
