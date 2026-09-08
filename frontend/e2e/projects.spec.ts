import { test, expect } from '@playwright/test';

// Projects module, end-to-end: an admin registers a client (unique 3-letter code),
// then creates a project under it and the system assigns a <CODE>-<NNN> id.
test.describe('Projects', () => {
  test('register a client, then create a project under it', async ({ page }) => {
    // Register a client (admin-only Clients tab).
    await page.goto('/m/projects/clients');
    await page.getByRole('button', { name: 'New client' }).click();
    const clientDialog = page.getByRole('dialog', { name: 'Register client' });
    await clientDialog.getByLabel('Name').fill('E2E Client');
    await clientDialog.getByLabel('Code').fill('ZZZ');
    await clientDialog.getByRole('button', { name: 'Register' }).click();
    await expect(page.getByRole('cell', { name: 'ZZZ', exact: true })).toBeVisible();

    // Create a project under it -> system-assigned code ZZZ-001.
    await page.goto('/m/projects');
    await page.getByRole('button', { name: 'New project' }).click();
    const projectDialog = page.getByRole('dialog', { name: 'New project' });
    // The dialog's Client picker is a searchable combobox: open, filter by code, pick it.
    const clientCombo = projectDialog.getByRole('combobox', { name: /Client/ });
    await clientCombo.click();
    await clientCombo.fill('ZZZ');
    await projectDialog.getByRole('listbox').getByRole('option').first().click();
    await projectDialog.getByLabel('Name').fill('E2E Project');
    await projectDialog.getByRole('button', { name: 'Create project' }).click();

    // Target the register CODE cell exactly — a success toast also contains the code, and
    // the row's Edit button carries aria-label "Edit ZZZ-001", so a non-exact match is
    // strict-mode-ambiguous. `exact` pins it to the `<td>ZZZ-001</td>` code cell alone.
    await expect(page.getByRole('cell', { name: 'ZZZ-001', exact: true })).toBeVisible();
  });
});
