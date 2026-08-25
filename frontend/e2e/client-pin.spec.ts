import { test, expect } from '@playwright/test';

// Client challan-QR PIN, end-to-end through the REAL UI — the one screen where staff set the
// per-client PIN that (with the challan number) forms the QR-viewer password. Registers a
// client, opens its detail, and drives the edit modal: the 8-char minimum is enforced (Save
// stays disabled + inline error), and a valid PIN flips the "Challan QR PIN" badge to "Set".
// (The public /d/{token} viewer itself is covered by the backend suite; this closes the one
// real browser-clicking gap — the admin screen.)
test.describe('Client challan-QR PIN', () => {
  test('set a client access PIN via the edit modal (min-length enforced)', async ({ page }) => {
    // Register a client (admin-only Clients tab) with a unique 3-letter code.
    await page.goto('/m/projects/clients');
    await page.getByRole('button', { name: 'New client' }).click();
    const dialog = page.getByRole('dialog', { name: 'Register client' });
    await dialog.getByLabel('Name').fill('QR PIN Client');
    await dialog.getByLabel('Code').fill('QRP');
    await dialog.getByRole('button', { name: 'Register' }).click();

    // Open its detail page — the client NAME is the row link.
    await page.getByRole('link', { name: 'QR PIN Client' }).click();
    await expect(page.getByRole('button', { name: 'Edit client' })).toBeVisible();

    // Scope the "Challan QR PIN" status badge; it starts "Not set".
    const pinStatus = page.getByText('Challan QR PIN').locator('..');
    await expect(pinStatus.getByText('Not set')).toBeVisible();

    // Open the edit modal.
    await page.getByRole('button', { name: 'Edit client' }).click();
    const edit = page.getByRole('dialog', { name: 'Edit client' });
    const pin = edit.getByLabel(/Access PIN/);

    // A too-short PIN keeps Save disabled and shows the 8-32 inline error.
    await pin.fill('short'); // 5 chars
    await expect(edit.getByText('8-32 characters.')).toBeVisible();
    await expect(edit.getByRole('button', { name: 'Save' })).toBeDisabled();

    // A valid PIN enables Save; saving flips the badge to "Set".
    await pin.fill('qrpin1234'); // 9 chars
    await edit.getByRole('button', { name: 'Save' }).click();
    await expect(pinStatus.getByText('Set', { exact: true })).toBeVisible();
  });
});
