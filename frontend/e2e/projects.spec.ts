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
    const clientValue = await projectDialog
      .locator('option', { hasText: 'ZZZ' })
      .getAttribute('value');
    await projectDialog.getByLabel('Client').selectOption(clientValue!);
    await projectDialog.getByLabel('Name').fill('E2E Project');
    await projectDialog.getByRole('button', { name: 'Create project' }).click();

    await expect(page.getByText('ZZZ-001')).toBeVisible();
  });
});
