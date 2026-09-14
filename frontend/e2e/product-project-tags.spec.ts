import { test, expect, type Page, type Locator, type APIRequestContext } from '@playwright/test';

/**
 * Sales Orders — "tag products to projects" (the curated PO picker) driven END-TO-END
 * through the real SPA + FastAPI backend as the default Administrator.
 *
 * Tagging a product to a project curates that project's PO product picker: an empty
 * search box surfaces the project's tagged products; typing still reaches the full
 * catalogue. This spec proves the whole loop through the UI:
 *   1. on the project detail, TAG one of two products via the Products section,
 *   2. the tagged product lists (and the other one does NOT),
 *   3. on that project's New PO form, opening a line's product picker with an EMPTY
 *      query shows the tagged product and NOT the untagged one (the curation),
 *   4. REMOVE the tag -> it drops from the project's list.
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// Distinct 3-letter client code (bootstrap BRI, projects ZZZ, expense EXC, expense-alloc ALC,
// sales-orders SPN, sales-orders-rbac RBC, GEN overhead, billing BLG/BRB/CNT/LNE) so it never
// collides in the shared sqlite DB.
const CLIENT_CODE = 'TAG';
const CLIENT_NAME = 'Tag Client';
const PROJECT_CODE = `${CLIENT_CODE}-001`;
// Distinctive product keywords so the picker assertions can't collide with other specs.
const TAGGED_PRODUCT = 'Tagworthy Widget';
const UNTAGGED_PRODUCT = 'Untagged Gadget';

async function apiPost(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.post(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `POST ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

/** Pick from a SearchableSelect combobox: open, filter, click the first option in its listbox. */
async function pickCombo(root: Page | Locator, nameRe: RegExp, filterText: string): Promise<void> {
  const combo = root.getByRole('combobox', { name: nameRe });
  await combo.click();
  await combo.fill(filterText);
  await root.getByRole('listbox').getByRole('option').first().click();
}

test.describe('Sales Orders — tag products to projects (admin end-to-end)', () => {
  test('tag a product to a project; its PO picker curates to the tagged product', async ({
    page,
    request,
  }) => {
    // ---- seed two products + a client + a project via the real API ----
    await apiPost(request, '/products', { name: TAGGED_PRODUCT, uom: 'PCS', brand: 'Acme' });
    await apiPost(request, '/products', { name: UNTAGGED_PRODUCT, uom: 'PCS', brand: 'Acme' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT_NAME, code: CLIENT_CODE });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Tag Project' });
    expect(project.code).toBe(PROJECT_CODE);

    // ---- 1. tag TAGGED_PRODUCT on the project detail's Products section ----
    await page.goto(`/m/projects/${project.id}`);
    await expect(page.getByRole('heading', { name: 'Tagged products' })).toBeVisible();
    // Before tagging, the empty state shows.
    await expect(page.getByText(/No products tagged to this project yet/)).toBeVisible();

    await pickCombo(page, /Add product/i, 'Tagworthy');

    // ---- 2. the tagged product lists; the untagged one does not ----
    const productsHeading = page.getByRole('heading', { name: 'Tagged products' });
    const productsSection = page.locator('section').filter({ has: productsHeading });
    const taggedRow = productsSection.getByRole('row').filter({ hasText: TAGGED_PRODUCT });
    await expect(taggedRow).toHaveCount(1);
    await expect(productsSection.getByRole('row').filter({ hasText: UNTAGGED_PRODUCT })).toHaveCount(0);

    // ---- 3. the project's New PO product picker curates to the tagged product ----
    await page.goto('/m/sales_orders/new');
    await expect(page.getByRole('heading', { name: 'New purchase order' })).toBeVisible();
    await pickCombo(page, /^Client ?\*/, CLIENT_NAME);
    await pickCombo(page, /^Project ?\*/, PROJECT_CODE);

    // Open the first line's Product picker WITHOUT typing — the curated default = tagged only.
    const productCombo = page.getByLabel('Product').first();
    await productCombo.click();
    const listbox = page.getByRole('listbox');
    await expect(listbox.getByRole('option', { name: new RegExp(TAGGED_PRODUCT) })).toBeVisible();
    await expect(listbox.getByRole('option', { name: new RegExp(UNTAGGED_PRODUCT) })).toHaveCount(0);
    // …and typing still reaches the full catalogue (the untagged product becomes findable).
    await productCombo.fill('Untagged');
    await expect(listbox.getByRole('option', { name: new RegExp(UNTAGGED_PRODUCT) })).toBeVisible();

    // ---- 4. remove the tag -> it drops from the project's list ----
    await page.goto(`/m/projects/${project.id}`);
    const taggedRowAgain = page
      .locator('section')
      .filter({ has: page.getByRole('heading', { name: 'Tagged products' }) })
      .getByRole('row')
      .filter({ hasText: TAGGED_PRODUCT });
    await expect(taggedRowAgain).toHaveCount(1);
    await taggedRowAgain.getByRole('button', { name: 'Remove' }).click();
    await expect(page.getByText(/No products tagged to this project yet/)).toBeVisible();
  });
});
