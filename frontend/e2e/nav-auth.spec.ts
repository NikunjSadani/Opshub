import { test, expect } from '@playwright/test';

// The dev role switcher (mock auth) drives client-side role gating. Switching to a
// non-admin IN PLACE (no reload, so the mock role persists) must both hide the admin
// nav entry and flip the admin surface to the "Not authorized" panel. Server-side
// RBAC is the real gate (covered by the backend 403s); this covers the UX gating.
test.describe('Admin-surface gating', () => {
  test('Users nav + screen gate to admins only', async ({ page }) => {
    await page.goto('/admin/users');
    await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Users' })).toBeVisible();

    // Switch the dev role to a non-admin in place -> RequireRole re-evaluates.
    await page.getByLabel('Dev role switcher').selectOption('OPERATIONS');

    await expect(page.getByText('Not authorized')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Users' })).toHaveCount(0);
  });
});
