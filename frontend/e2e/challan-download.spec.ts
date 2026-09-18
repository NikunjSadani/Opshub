import { test, expect, request } from '@playwright/test';
import { statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const TEMPLATE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'template.xlsx');

// A DEDICATED series for this spec, so it can issue + void + download its own challans
// WITHOUT touching series "L" — the sequence `challan.spec.ts` (which runs after this
// file, and hard-asserts .../L/000001) depends on being the first to mint. The numbering
// engine refuses an unconfigured series, so we seed it via the real admin API first.
const SERIES = 'D';

/** Indian financial year label (Apr–Mar) for "now", computed in IST — mirrors the
 *  backend's `numbering.current_fy()` so we seed/preview the exact FY the generate
 *  flow allocates into. Computed relative to today (never a hard-coded FY that rots). */
function currentIstFy(): string {
  const nowUtcMs = Date.now();
  const ist = new Date(nowUtcMs + 5.5 * 60 * 60 * 1000); // UTC+5:30
  const y = ist.getUTCFullYear();
  const startYear = ist.getUTCMonth() + 1 >= 4 ? y : y - 1; // getUTCMonth is 0-based
  const pad = (n: number) => String(n % 100).padStart(2, '0');
  return `${pad(startYear)}-${pad(startYear + 1)}`;
}

/** The trailing zero-padded number of a formatted challan (".../D/000002" -> 2). */
function numberOf(formatted: string): number {
  const m = /\/(\d+)$/.exec(formatted);
  if (!m) throw new Error(`unparseable challan number: ${formatted}`);
  return parseInt(m[1], 10);
}

// End-to-end, through the real SPA + backend, for the range/list bulk-download feature:
// seed a dedicated series -> upload+generate 2 challans in it -> VOID one -> Preview the
// (series, FY, list) selection and assert the count + the voided-skip notice -> then
// download BOTH packagings (separate ZIP, merged 2-up PDF) and assert each starts a real
// download whose file is non-empty with the right extension. Nothing is fabricated: the
// challans are minted by the real generate flow and discovered from the real register API.
test.describe('Challan range/list download', () => {
  test('seed series -> generate -> void one -> preview skip notice -> download ZIP + merged PDF', async ({
    page,
  }) => {
    const fy = currentIstFy();

    // --- Setup: configure the dedicated series via the real admin API (dev-auth maps
    // any bearer to the seeded ADMIN). Tolerate 409 so a reused (non-fresh) backend,
    // where the counter already exists, still proceeds. ---
    const api = await request.newContext({
      baseURL: 'http://localhost:8000',
      extraHTTPHeaders: { Authorization: 'Bearer e2e-admin' },
    });
    const seed = await api.post('/api/v1/numbering/seed', {
      data: { series: SERIES, fy, last_number: 0 },
    });
    if (!seed.ok() && seed.status() !== 409) {
      throw new Error(`series seed failed: HTTP ${seed.status()} ${await seed.text()}`);
    }

    // --- Upload + validate the template's 2 example rows. ---
    await page.goto('/m/document_automation/new');
    await page.setInputFiles('#challan-file', TEMPLATE);
    await page.getByRole('button', { name: 'Upload & validate' }).click();

    // --- Number & generate INTO our dedicated series (now a dropdown of configured series;
    // 'D' is seeded active by the e2e bootstrap). ---
    await page.getByLabel('Series').selectOption(SERIES);
    const generate = page.getByRole('button', { name: /Generate 2 challans?/ });
    await expect(generate).toBeVisible();
    await generate.click();

    // Generation is async; completion = the batch's ZIP artifact becomes downloadable.
    await expect(page.getByRole('button', { name: 'Download ZIP' })).toBeEnabled({
      timeout: 20_000,
    });

    // --- Discover the two minted numbers from the real register API (no hard-coded
    // sequence assumptions — robust even on a reused backend). ---
    const listRes = await api.get(
      `/api/v1/challan/challans?series=${SERIES}&fy=${fy}&status=ISSUED`,
    );
    expect(listRes.ok()).toBeTruthy();
    const issued = (await listRes.json()) as Array<{ number: string; status: string }>;
    expect(issued.length).toBeGreaterThanOrEqual(2);
    // Two lowest ISSUED numbers = the pair this run just minted.
    const pair = issued.map((c) => c.number).sort((a, b) => numberOf(a) - numberOf(b));
    const voidedFormatted = pair[0];
    const voidedNumber = numberOf(voidedFormatted);
    const keptNumber = numberOf(pair[1]);

    // --- Void the LOWER one through the real Register UI, exercising the skip path. ---
    await page.goto('/m/document_automation/register');
    await page.getByLabel('Series').fill(SERIES); // isolate our series in the register
    const voidRow = page.getByRole('row', { name: voidedFormatted });
    await expect(voidRow).toBeVisible();
    await voidRow.getByRole('button', { name: 'Void' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Reason').fill('E2E download-skip test');
    await dialog.getByRole('button', { name: 'Void challan' }).click();
    await expect(
      page.getByRole('row', { name: voidedFormatted }).getByText('VOID'),
    ).toBeVisible();

    // --- Download tab: preview the (series, FY, explicit list of both numbers). ---
    await page.goto('/m/document_automation/download');
    await page.getByLabel('Series').fill(SERIES);
    await page.getByLabel('Financial year').fill(fy);
    await page.getByLabel('Challan numbers').fill(`${voidedNumber}, ${keptNumber}`);
    await page.getByRole('button', { name: 'Preview' }).click();

    // Count reflects only the ISSUED one (the void is excluded)...
    const previewLine = page.getByText(/challan[s]? will download/);
    await expect(previewLine).toBeVisible();
    await expect(previewLine).toContainText('1');

    // ...and the voided number is called out explicitly (never silently dropped).
    const skipNotice = page.getByText(/Skipping voided challan/);
    await expect(skipNotice).toBeVisible();
    await expect(skipNotice).toContainText(String(voidedNumber));

    // --- Download #1: separate PDFs (ZIP) — the default packaging. ---
    const downloadBtn = page.getByRole('button', { name: 'Download' });
    await expect(downloadBtn).toBeEnabled();
    const [zip] = await Promise.all([
      page.waitForEvent('download'),
      downloadBtn.click(),
    ]);
    expect(zip.suggestedFilename()).toMatch(/\.zip$/);
    const zipPath = await zip.path();
    expect(zipPath).toBeTruthy();
    expect(statSync(zipPath!).size).toBeGreaterThan(0);

    // --- Download #2: merged 2-up PDF. Switching packaging does not clear the
    // committed preview, so the Download button stays enabled. ---
    await page.getByRole('radio', { name: /Merged/ }).check();
    await expect(downloadBtn).toBeEnabled();
    const [pdf] = await Promise.all([
      page.waitForEvent('download'),
      downloadBtn.click(),
    ]);
    expect(pdf.suggestedFilename()).toMatch(/\.pdf$/);
    const pdfPath = await pdf.path();
    expect(pdfPath).toBeTruthy();
    expect(statSync(pdfPath!).size).toBeGreaterThan(0);

    await api.dispose();
  });
});
