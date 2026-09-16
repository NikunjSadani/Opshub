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

async function apiPatch(request: APIRequestContext, path: string, data: unknown): Promise<any> {
  const res = await request.patch(`/api/v1${path}`, { headers: DEV_ADMIN, data });
  expect(res.ok(), `PATCH ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
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

  test('a project pricing template pre-fills a New PO line (qty blank)', async ({
    page,
    request,
  }) => {
    // Distinct codes/keywords so this never collides with the tagging test in the shared DB.
    const CLIENT = { code: 'TPL', name: 'Template Client' };
    const PRODUCT = 'Templatable Sprocket';

    // ---- seed product + client + project; TAG the product, then set a template price ----
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS', brand: 'Acme' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT.name, code: CLIENT.code });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Template Project' });
    await apiPost(request, '/project-products', { project_id: project.id, product_id: product.id });
    // ₹12,345.00 client sell → the line input should pre-fill "12345.00".
    await apiPatch(request, `/project-products/${project.id}/${product.id}`, {
      client_sell_price_paise: 1234500,
      cost_price_paise: 1000000,
      tax_rate: '18',
    });

    // ---- New PO for that project → "Load this project's products" → line pre-filled ----
    await page.goto('/m/sales_orders/new');
    await expect(page.getByRole('heading', { name: 'New purchase order' })).toBeVisible();
    await pickCombo(page, /^Client ?\*/, CLIENT.name);
    await pickCombo(page, /^Project ?\*/, `${CLIENT.code}-001`);

    await page.getByRole('button', { name: /Load this project's products/i }).click();

    // The template money maps onto the line's rupee inputs; ordered qty is left blank.
    await expect(page.getByLabel(/Client sell/i).first()).toHaveValue('12345.00');
    await expect(page.getByLabel(/Our CP/i).first()).toHaveValue('10000.00');
    await expect(page.getByLabel(/Ordered qty/i).first()).toHaveValue('');
  });

  test('edit a product pricing template through the modal (admin sees the actual-margin fields)', async ({
    page,
    request,
  }) => {
    const CLIENT = { code: 'PED', name: 'Pricing Edit Client' };
    const PRODUCT = 'Editable Cog';
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT.name, code: CLIENT.code });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Pricing Edit Project' });
    await apiPost(request, '/project-products', { project_id: project.id, product_id: product.id });

    await page.goto(`/m/projects/${project.id}`);
    await page.getByRole('button', { name: `Edit ${PRODUCT}` }).click();
    const dialog = page.getByRole('dialog', { name: /Pricing/ });
    await expect(dialog).toBeVisible();
    // As admin the ACTUAL (margin) inputs are present and editable.
    await expect(dialog.getByLabel(/Actual sell/i)).toBeVisible();
    await expect(dialog.getByLabel(/Actual freight/i)).toBeVisible();

    await dialog.getByLabel(/Client sell/i).fill('500');
    await dialog.getByLabel(/Our CP/i).fill('400');
    await dialog.getByLabel(/Actual sell/i).fill('450');
    await dialog.getByLabel(/Tax rate/i).fill('18');
    await dialog.getByRole('button', { name: 'Save pricing' }).click();
    await expect(dialog).toBeHidden();

    // The saved client-sell price now shows in the row (a fresh GET reflects the PATCH).
    const row = page.getByRole('row').filter({ hasText: PRODUCT });
    await expect(row).toContainText('500.00');
  });

  test('load a project pricing template into a New PO, set qty, and submit it', async ({
    page,
    request,
  }) => {
    const CLIENT = { code: 'PLS', name: 'PO Load Client' };
    const PRODUCT = 'Loadable Bolt';
    const PO_NUMBER = 'PO-PLS-1';
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT.name, code: CLIENT.code });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'PO Load Project' });
    await apiPost(request, '/project-products', { project_id: project.id, product_id: product.id });
    await apiPatch(request, `/project-products/${project.id}/${product.id}`, {
      client_sell_price_paise: 55000, cost_price_paise: 40000, tax_rate: '18',
    });

    await page.goto('/m/sales_orders/new');
    await expect(page.getByRole('heading', { name: 'New purchase order' })).toBeVisible();
    await page.getByLabel('PO number').fill(PO_NUMBER);
    await pickCombo(page, /^Client ?\*/, CLIENT.name);
    await pickCombo(page, /^Project ?\*/, `${CLIENT.code}-001`);
    await page.getByLabel('PO date').fill(new Date().toISOString().slice(0, 10));

    await page.getByRole('button', { name: /Load this project's products/i }).click();
    // The pre-filled line carries the template price; the operator supplies the quantity.
    await expect(page.getByLabel(/Client sell/i).first()).toHaveValue('550.00');
    await page.getByLabel(/Ordered qty/i).first().fill('7');

    await page.getByRole('button', { name: 'Create purchase order' }).click();
    // On success the form navigates to the PO detail — the PO was created from the template.
    await expect(page.getByRole('heading', { name: `PO ${PO_NUMBER}` })).toBeVisible();
  });

  test('a NON-admin never sees the actual-margin fields (pricing editor + loaded PO line)', async ({
    page,
    request,
  }) => {
    const CLIENT = { code: 'MSK', name: 'Masking Client' };
    const PRODUCT = 'Secret-Margin Widget';
    // Seed a template WITH the admin-only actuals set (by dev-admin), so we prove they are
    // HIDDEN from a non-admin — not merely unset.
    const product = await apiPost(request, '/products', { name: PRODUCT, uom: 'PCS' });
    const client = await apiPost(request, '/projects/clients', { name: CLIENT.name, code: CLIENT.code });
    const project = await apiPost(request, '/projects', { client_id: client.id, name: 'Masking Project' });
    await apiPost(request, '/project-products', { project_id: project.id, product_id: product.id });
    await apiPatch(request, `/project-products/${project.id}/${product.id}`, {
      client_sell_price_paise: 90000, cost_price_paise: 70000,
      sell_price_paise: 85000, freight_paise: 3000, // ACTUAL margin — admin-only
    });

    // Give the seeded dev-operator a role with sales_orders OPERATE + projects VIEW but NO
    // platform IAM, so it can reach both surfaces yet is masked from the actuals.
    const users = await (await request.get('/api/v1/users', { headers: DEV_ADMIN })).json();
    const operator = users.find((u: { email: string }) => u.email === 'operator@opshub.local');
    expect(operator, 'seeded operator@opshub.local must exist').toBeTruthy();
    const role = await apiPost(request, '/roles', {
      name: 'Sales Ops Masking (E2E)',
      description: 'E2E: sales_orders OPERATE + projects VIEW, NO platform IAM.',
      module_levels: { sales_orders: 'OPERATE', projects: 'VIEW' },
      platform: [],
    });
    const patch = await request.patch(`/api/v1/users/${operator.id}`, {
      headers: DEV_ADMIN, data: { role_id: role.id },
    });
    expect(patch.ok(), `role reassign -> ${patch.status()} ${await patch.text()}`).toBeTruthy();

    // Switch to the non-admin IN PLACE (a page.goto would remount the shim back to dev-admin)
    // and navigate by clicking, so every assertion is against the served permission set.
    await page.goto('/');
    await page.getByLabel('Dev user switcher').selectOption({ label: 'Challan Operator' });
    const sidebar = page.getByRole('navigation');

    // ---- pricing editor: the actual-margin inputs are absent for a non-admin ----
    await sidebar.getByRole('link', { name: 'Projects' }).click();
    await page.getByRole('link', { name: `${CLIENT.code}-001` }).click();
    await expect(page.getByRole('heading', { name: 'Tagged products' })).toBeVisible();
    await page.getByRole('button', { name: `Edit ${PRODUCT}` }).click();
    const dialog = page.getByRole('dialog', { name: /Pricing/ });
    await expect(dialog.getByLabel(/Client sell/i)).toBeVisible(); // visible tier IS shown
    await expect(dialog.getByLabel(/Actual sell/i)).toHaveCount(0); // margin is NOT
    await expect(dialog.getByLabel(/Actual freight/i)).toHaveCount(0);
    await dialog.getByRole('button', { name: /Cancel|Close/ }).first().click();

    // ---- loaded PO line: the line carries the visible price but NO actual-margin inputs ----
    await sidebar.getByRole('link', { name: 'Purchase Orders' }).click();
    await page.getByRole('link', { name: 'New PO' }).click();
    await expect(page.getByRole('heading', { name: 'New purchase order' })).toBeVisible();
    await pickCombo(page, /^Client ?\*/, CLIENT.name);
    await pickCombo(page, /^Project ?\*/, `${CLIENT.code}-001`);
    await page.getByRole('button', { name: /Load this project's products/i }).click();
    await expect(page.getByLabel(/Client sell/i).first()).toHaveValue('900.00'); // visible tier pre-filled
    await expect(page.getByLabel(/Actual sell/i)).toHaveCount(0); // margin never rendered
    await expect(page.getByLabel(/Actual freight/i)).toHaveCount(0);
  });
});
