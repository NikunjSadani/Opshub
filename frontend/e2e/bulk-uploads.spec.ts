import { test, expect, type APIRequestContext } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Sales Orders — the two Excel BULK UPLOADS driven END-TO-END through the real SPA +
 * FastAPI backend as the default Administrator (a real multipart file upload):
 *   1. Products bulk upload — a fixture .xlsx creates a product (which then lists in the
 *      register), proving the download-gated upload + parse + outcome render.
 *   2. Per-project Pricing bulk upload — a fixture .xlsx prices an existing product (matched
 *      by its code) for a project; the outcome lists it as priced.
 *
 * The fixtures (e2e/fixtures/products-bulk.xlsx, pricing-bulk.xlsx) carry the canonical
 * headers the parsers accept; the pricing sheet references a product seeded here with the
 * known code BULKSEED1.
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };
const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), 'fixtures');
const PRODUCTS_XLSX = join(FIXTURES, 'products-bulk.xlsx');
const PRICING_XLSX = join(FIXTURES, 'pricing-bulk.xlsx');

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

test.describe('Sales Orders — Excel bulk uploads (admin end-to-end)', () => {
  test('Products bulk upload creates a product that then lists in the register', async ({
    page,
  }) => {
    await page.goto('/m/sales_orders/products/upload');
    await expect(page.getByRole('heading', { name: /bulk upload products/i })).toBeVisible();

    await page.setInputFiles('#products-file', PRODUCTS_XLSX);
    await page.getByRole('button', { name: 'Upload', exact: true }).click();

    // The outcome summary renders; one product was created (0 errors).
    await expect(page.getByRole('heading', { name: 'Upload summary' })).toBeVisible();
    await expect(page.getByText(/1 created/i)).toBeVisible();

    // …and the created product now lists in the Products register.
    await page.goto('/m/sales_orders/products');
    await expect(page.getByText('E2E Bulk Widget')).toBeVisible();
  });

  test('Pricing bulk upload prices an existing product for a project', async ({ page, request }) => {
    // Seed the product with a KNOWN code (matches the pricing fixture's product_code) + a
    // client + project. The upload tags + prices it (no pre-tag needed).
    await apiPost(request, '/products', { name: 'Bulk Seed Product', code: 'BULKSEED1', uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: 'Bulk Pricing Client', code: 'PBK' });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Bulk Pricing Project' });

    await page.goto(`/m/projects/${project.id}`);
    await expect(page.getByRole('heading', { name: 'Tagged products' })).toBeVisible();

    await page.getByRole('button', { name: /Upload pricing \.xlsx/i }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#pricing-file').setInputFiles(PRICING_XLSX);
    await dialog.getByRole('button', { name: 'Upload', exact: true }).click();

    // The outcome shows one product priced, with its code, and no errors.
    await expect(dialog.getByText(/Priced: 1/i)).toBeVisible();
    await expect(dialog.getByText('BULKSEED1')).toBeVisible();
  });
});
