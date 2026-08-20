import { test, expect } from '@playwright/test';

/**
 * RBAC v2 role-gating, end-to-end through the REAL SPA + backend.
 *
 * The dev switcher ("Act as (dev)") picks which SEEDED user we act as; that sets
 * an `X-Dev-Uid` header so `GET /me` returns that user's REAL permissions and the
 * UI re-gates live. We switch IN PLACE (never page.goto/reload — a reload remounts
 * the mock provider back to `dev-admin`) and navigate purely by CLICKING nav links,
 * so every assertion is against the actually-served permission set.
 *
 * Seeded roles (backend app/seed.py + roles_builtin.py):
 *   Administrator   — is_system: MANAGE everywhere + all platform perms (incl. `iam`).
 *   Challan Operator— document_automation: OPERATE (no `iam`, no MANAGE).
 *   Viewer          — every module VIEW only (no `iam`, no MANAGE).
 *
 * Server-side RBAC is the real gate (backend 403s); this proves the UX gating that
 * keeps a user out of dead-ends re-computes correctly as the role changes.
 */
test.describe('RBAC v2 role-gating', () => {
  test('admin sees admin surfaces; viewer + operator are gated live', async ({ page }) => {
    const switcher = page.getByLabel('Dev user switcher');
    const usersLink = page.getByRole('link', { name: 'Users' });
    const rolesLink = page.getByRole('link', { name: 'Roles' });

    // ---- 1. Administrator (default acting user) ----
    await page.goto('/');

    // The Administration section (governed by the `iam` platform permission) is shown.
    await expect(usersLink).toBeVisible();
    await expect(rolesLink).toBeVisible();

    // Client-side nav (click the link, no goto) to the Roles admin screen.
    await rolesLink.click();
    await expect(page).toHaveURL(/\/admin\/roles$/);
    await expect(page.getByRole('heading', { name: 'Roles' })).toBeVisible();
    // The built-in Administrator role is listed, flagged as a system role.
    const adminRoleRow = page.getByRole('row', { name: /Administrator/ });
    await expect(adminRoleRow).toBeVisible();
    await expect(adminRoleRow.getByText('Built-in')).toBeVisible();

    // ---- 2. Viewer (switched IN PLACE — no reload) ----
    await switcher.selectOption({ label: 'Viewer' });

    // The admin nav links disappear as `GET /me` re-resolves without `iam` (live re-gate).
    await expect(usersLink).toHaveCount(0);
    await expect(rolesLink).toHaveCount(0);

    // ---- 4. Viewer write is blocked (checked while Viewer is active) ----
    // Reach a real challan surface by CLICKING the sidebar module link (client-side).
    await page.getByRole('link', { name: 'Delivery Challan' }).click();
    await expect(page).toHaveURL(/\/m\/document_automation/);
    // Master Data (edits shared config) needs MANAGE — absent for a Viewer.
    await expect(page.getByRole('link', { name: 'Master Data' })).toHaveCount(0);
    // On the Register, the Void action (a MANAGE-only mutation) is absent for a Viewer.
    await page.getByRole('link', { name: 'Register', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Register' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Void' })).toHaveCount(0);

    // ---- 3. Challan Operator (switched IN PLACE — no reload) ----
    await switcher.selectOption({ label: 'Challan Operator' });

    // Still no admin surface (Operator lacks `iam`).
    await expect(usersLink).toHaveCount(0);
    await expect(rolesLink).toHaveCount(0);
    // Master Data still absent (OPERATE < MANAGE) ...
    await expect(page.getByRole('link', { name: 'Master Data' })).toHaveCount(0);
    // ... but a normal challan surface (Register) remains reachable and rendered.
    await expect(page.getByRole('link', { name: 'Register', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Register' })).toBeVisible();
  });
});
