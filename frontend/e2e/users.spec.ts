import { test, expect } from '@playwright/test';

// User Management, end-to-end through the real SPA + backend (dev-auth = admin).
test.describe('User Management', () => {
  test('lists seeded users, invites a new one, and blocks last-admin lockout', async ({
    page,
  }) => {
    await page.goto('/admin/users');
    await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();

    // Seeded users are listed (seed now provisions admin/manager/operator/viewer).
    await expect(page.getByText('admin@opshub.local')).toBeVisible();
    await expect(page.getByText('manager@opshub.local')).toBeVisible();

    // --- Invite a new user ---
    await page.getByRole('button', { name: 'Invite user' }).click();
    const invite = page.getByRole('dialog', { name: 'Invite user' });
    await invite.getByPlaceholder('e.g. jane@gifsy.in').fill('jane.e2e@gifsy.in');
    await invite.getByPlaceholder('e.g. Jane Doe').fill('Jane E2E');
    await invite.getByRole('button', { name: 'Send invite' }).click();

    // Success view: honest one-time-link copy (no real account locally -> null link).
    await expect(page.getByRole('heading', { name: 'User invited' })).toBeVisible();
    await expect(page.getByText(/no setup link yet/i)).toBeVisible();
    await page.getByRole('button', { name: 'Done' }).click();

    // The new user appears in the list, flagged not-yet-provisioned.
    const janeRow = page.getByRole('row', { name: /jane\.e2e@gifsy\.in/ });
    await expect(janeRow).toBeVisible();
    await expect(janeRow.getByText('setup pending')).toBeVisible();

    // --- Last-admin / self-lockout guard ---
    const adminRow = page.getByRole('row', { name: /admin@opshub\.local/ });
    await adminRow.getByRole('button', { name: 'Edit' }).click();
    const edit = page.getByRole('dialog', { name: /Edit admin@opshub\.local/ });
    await edit.getByRole('button', { name: 'Disable' }).click();
    await edit.getByRole('button', { name: 'Save changes' }).click();

    // The guard rejects it (409): the dialog stays open and the admin stays Active.
    await expect(edit).toBeVisible();
    await expect(adminRow.getByText('Active')).toBeVisible();
  });
});
