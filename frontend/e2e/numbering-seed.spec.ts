import { test, expect } from '@playwright/test';

/** Indian FY label (Apr–Mar) for "now" in IST — mirrors the backend's `current_fy()`.
 *  Computed relative to today so it never rots into a hard-coded year. */
function currentIstFy(): string {
  const ist = new Date(Date.now() + 5.5 * 60 * 60 * 1000); // UTC+5:30
  const y = ist.getUTCFullYear();
  const startYear = ist.getUTCMonth() + 1 >= 4 ? y : y - 1; // getUTCMonth is 0-based
  const pad = (n: number) => String(n % 100).padStart(2, '0');
  return `${pad(startYear)}-${pad(startYear + 1)}`;
}

// End-to-end through the real SPA + backend for the new "Set starting number" admin
// action: an admin opens the Numbering screen, seeds a DEDICATED fresh series via the
// UI (not the API), and the counters grid shows the new high-water mark. This is the
// runtime proof that the seed UI is wired to the real POST /numbering/seed contract.
// A dedicated series ('SEEDUI') keeps it isolated from the specs that mint into L/D.
test.describe('Numbering — set starting number (seed UI)', () => {
  test('admin seeds a fresh series through the UI -> counter appears', async ({ page }) => {
    const fy = currentIstFy();
    const SERIES = 'SEEDUI';

    // Dev-auth maps any bearer to the seeded ADMIN, so the SPA loads with MANAGE.
    await page.goto('/m/document_automation/numbering');

    // The admin-only action is present.
    const openBtn = page.getByRole('button', { name: 'Set starting number' });
    await expect(openBtn).toBeVisible();
    await openBtn.click();

    // Fill the modal. Scope to the dialog (the page also has Series/FY filter inputs),
    // and anchor each label with ^ — Playwright folds the field HINT into the accessible
    // name, and several hints contain the word "series", so a bare 'Series' is ambiguous.
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel(/^Series/).fill(SERIES);
    await dialog.getByLabel(/^Financial year/).fill(fy);
    await dialog.getByLabel(/^Last issued number/).fill('0');

    // Before committing, the preview shows the resulting first number.
    await expect(dialog.getByText(/never reissued/i)).toBeVisible();

    // Submit (the footer button inside the dialog, not the header opener).
    await dialog.getByRole('button', { name: 'Set starting number' }).click();

    // The modal closes and the counters grid refreshes to show the new series's card
    // (a real backend round-trip: POST /numbering/seed then GET /numbering/counters).
    // The success toast also echoes the series, so pin to the counter card via its
    // unique "Last issued number" caption.
    await expect(dialog).toBeHidden();
    const seededCard = page
      .locator('div.rounded-xl')
      .filter({ hasText: SERIES })
      .filter({ hasText: 'Last issued number' });
    await expect(seededCard).toBeVisible();
    await expect(seededCard).toContainText(fy); // card labels the FY it configured
  });
});
