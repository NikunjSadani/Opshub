import { test, expect, type APIRequestContext } from '@playwright/test';

/**
 * Logistics tracker — Wave 3, delivery tracking driven END-TO-END through the REAL
 * SPA + FastAPI backend as the default Administrator (MANAGE everywhere).
 *
 * A shipment's `challan_number` is FREE TEXT — it does not require a real challan
 * (the backend resolves `challan_id` if the number matches an issued challan, else
 * leaves it null and never drops the row). So this spec is fully self-contained: it
 * never needs to generate a challan.
 *
 * The whole loop, purely through the UI (the bulk .xlsx is built in-memory below):
 *   1. create a shipment on the manual form (challan# + partner + status DISPATCHED)
 *      -> it lands in the register with the Dispatched status Badge,
 *   2. open its detail, flip the inline status control to DELIVERED -> it PERSISTS
 *      (the register + a re-opened detail both read Delivered via a fresh GET),
 *   3. bulk-upload an .xlsx: one row re-uses the SAME challan# (UPSERT -> updated:1)
 *      + one brand-new row (created:1) -> the upload summary renders both counts,
 *   4. delete the first shipment from its detail (MANAGE) via the ConfirmDialog
 *      -> it is gone from the register (the second, upload-created row remains).
 */

const DEV_ADMIN = { Authorization: 'Bearer e2e', 'X-Dev-Uid': 'dev-admin' };

// Distinct challan numbers (free text) so this spec's rows never collide with the
// logistics-rbac spec's seeded row or anything else in the shared sqlite DB.
const CHALLAN_MAIN = 'GIF/DC/E2E-LOG/900'; // created via the UI, updated via bulk, then deleted
const CHALLAN_NEW = 'GIF/DC/E2E-LOG/901'; //  created by the bulk upload (a NEW row)
const PARTNER = 'BlueDart';

// ---- a minimal .xlsx builder (a STORE-only ZIP of the OOXML parts openpyxl reads).
// There is no JS xlsx lib in the harness; the backend parses the first sheet, row 1 =
// headers, with inline-string cells — which openpyxl (read_only, data_only) reads
// verbatim. Rows are string[][]; row 0 is the header row.
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf: Buffer): number {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function colLetter(n: number): string {
  let s = '';
  n += 1;
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}
function xmlEscape(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function sheetXml(rows: string[][]): string {
  const body = rows
    .map((cells, r) => {
      const rowNum = r + 1;
      const cs = cells
        .map(
          (val, c) =>
            `<c r="${colLetter(c)}${rowNum}" t="inlineStr"><is><t xml:space="preserve">${xmlEscape(
              val,
            )}</t></is></c>`,
        )
        .join('');
      return `<row r="${rowNum}">${cs}</row>`;
    })
    .join('');
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${body}</sheetData></worksheet>`;
}
function buildXlsx(rows: string[][]): Buffer {
  const parts: [string, string][] = [
    [
      '[Content_Types].xml',
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>`,
    ],
    [
      '_rels/.rels',
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
    ],
    [
      'xl/workbook.xml',
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>`,
    ],
    [
      'xl/_rels/workbook.xml.rels',
      `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>`,
    ],
    ['xl/worksheets/sheet1.xml', sheetXml(rows)],
  ];

  const chunks: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of parts) {
    const nameBuf = Buffer.from(name, 'utf8');
    const data = Buffer.from(content, 'utf8');
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8); // method: store
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBuf, data);

    const cd = Buffer.alloc(46);
    cd.writeUInt32LE(0x02014b50, 0);
    cd.writeUInt16LE(20, 4);
    cd.writeUInt16LE(20, 6);
    cd.writeUInt16LE(0, 8);
    cd.writeUInt16LE(0, 10);
    cd.writeUInt16LE(0, 12);
    cd.writeUInt16LE(0, 14);
    cd.writeUInt32LE(crc, 16);
    cd.writeUInt32LE(data.length, 20);
    cd.writeUInt32LE(data.length, 24);
    cd.writeUInt16LE(nameBuf.length, 28);
    cd.writeUInt16LE(0, 30);
    cd.writeUInt16LE(0, 32);
    cd.writeUInt16LE(0, 34);
    cd.writeUInt16LE(0, 36);
    cd.writeUInt32LE(0, 38);
    cd.writeUInt32LE(offset, 42);
    central.push(cd, nameBuf);

    offset += local.length + nameBuf.length + data.length;
  }
  const cdBuf = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(parts.length, 8);
  eocd.writeUInt16LE(parts.length, 10);
  eocd.writeUInt32LE(cdBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20);
  return Buffer.concat([...chunks, cdBuf, eocd]);
}

/** Best-effort delete every shipment whose challan# exactly matches, as dev-admin. */
async function purgeShipments(request: APIRequestContext, challanNumber: string): Promise<void> {
  const res = await request.get(
    `/api/v1/logistics/shipments?challan_number=${encodeURIComponent(challanNumber)}`,
    { headers: DEV_ADMIN },
  );
  if (!res.ok()) return;
  const rows: { id: string | number }[] = await res.json();
  for (const row of rows) {
    await request
      .delete(`/api/v1/logistics/shipments/${row.id}`, { headers: DEV_ADMIN })
      .catch(() => {});
  }
}

test.describe('Logistics — Wave 3 (admin end-to-end)', () => {
  // Clean up regardless of where an assertion fails, so the run stays idempotent
  // (the DB is shared across specs and torn down only at run end).
  test.afterEach(async ({ request }) => {
    await purgeShipments(request, CHALLAN_MAIN);
    await purgeShipments(request, CHALLAN_NEW);
  });

  test('create -> register badge -> status update persists -> bulk upsert -> delete', async ({
    page,
    request,
  }) => {
    // The sidebar nav is scoped explicitly: on the home dashboard a module CARD also
    // links to /m/logistics (name "Logistics Operations Open →"), so an unscoped
    // getByRole('link', { name: 'Logistics' }) would be ambiguous.
    const logisticsNav = () => page.getByRole('navigation').getByRole('link', { name: 'Logistics' });

    // ================= 1. create a shipment through the manual form =================
    await page.goto('/m/logistics/new');
    await expect(page.getByRole('heading', { name: 'New shipment' })).toBeVisible();

    await page.getByLabel('Challan number').fill(CHALLAN_MAIN);
    await page.getByLabel('Delivery partner').fill(PARTNER);
    await page.getByLabel('Status').selectOption('DISPATCHED');
    await page.getByRole('button', { name: 'Create shipment' }).click();

    // On success the form navigates to the shipment detail.
    await expect(page.getByRole('heading', { name: `Shipment ${CHALLAN_MAIN}` })).toBeVisible();

    // ---- it lands in the register with the right partner + Dispatched Badge ----
    await page.getByRole('link', { name: 'Back to tracker' }).click();
    await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
    const mainRow = page.getByRole('row').filter({ hasText: CHALLAN_MAIN });
    await expect(mainRow).toHaveCount(1);
    await expect(mainRow).toContainText(PARTNER);
    await expect(mainRow).toContainText('Dispatched');

    // ================= 2. inline status update -> DELIVERED (persists) =================
    const openMainDetail = async () => {
      await logisticsNav().click();
      await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
      await page
        .getByRole('row')
        .filter({ hasText: CHALLAN_MAIN })
        .getByRole('link', { name: 'View' })
        .click();
      await expect(page.getByRole('heading', { name: `Shipment ${CHALLAN_MAIN}` })).toBeVisible();
    };

    await mainRow.getByRole('link', { name: 'View' }).click();
    await expect(page.getByRole('heading', { name: `Shipment ${CHALLAN_MAIN}` })).toBeVisible();

    // The inline control is a staged edit — pick DELIVERED, then press Update to PATCH.
    await page.getByLabel('Delivery status').selectOption('DELIVERED');
    await page.getByRole('button', { name: 'Update' }).click();
    await expect(page.getByText('Status updated.')).toBeVisible();

    // Persistence: the register (invalidated on update) reads Delivered via a fresh GET.
    await page.getByRole('link', { name: 'Back to tracker' }).click();
    await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: CHALLAN_MAIN })).toContainText('Delivered');

    // ...and a freshly re-opened detail shows the Delivered status Badge too. Scope to
    // the Status definition's <dd> — "Delivered" also appears as a <dt> ("Delivered on")
    // and as the inline control's selected <option>.
    await openMainDetail();
    const statusValue = page
      .getByText('Status', { exact: true })
      .locator('xpath=following-sibling::dd[1]');
    await expect(statusValue).toContainText('Delivered');

    // ================= 3. bulk .xlsx upsert (updated:1 + created:1) =================
    // Row 1 re-uses CHALLAN_MAIN (an UPSERT -> updated); row 2 is a NEW challan (created).
    const xlsx = buildXlsx([
      ['challan_number', 'delivery_partner', 'status', 'consignee_name'],
      [CHALLAN_MAIN, 'Delhivery', 'IN_TRANSIT', 'Alpha Traders'],
      [CHALLAN_NEW, 'Delhivery', 'DISPATCHED', 'Beta Stores'],
    ]);

    await logisticsNav().click();
    await page.getByRole('link', { name: 'Upload .xlsx' }).click();
    await expect(page.getByRole('heading', { name: 'Upload shipments' })).toBeVisible();

    await page.setInputFiles('#logistics-file', {
      name: 'partner-dump.xlsx',
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      buffer: xlsx,
    });
    await page.getByRole('button', { name: 'Upload', exact: true }).click();

    // The summary renders the created / updated / error counts.
    await expect(page.getByRole('heading', { name: 'Upload summary' })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText('Created: 1')).toBeVisible();
    await expect(page.getByText('Updated: 1')).toBeVisible();
    await expect(page.getByText('Row errors: 0')).toBeVisible();

    // The upsert did the right thing: the NEW challan is now a register row.
    await logisticsNav().click();
    await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: CHALLAN_NEW })).toHaveCount(1);

    // ================= 4. delete the first shipment (MANAGE) via the dialog =================
    await page
      .getByRole('row')
      .filter({ hasText: CHALLAN_MAIN })
      .getByRole('link', { name: 'View' })
      .click();
    await expect(page.getByRole('heading', { name: `Shipment ${CHALLAN_MAIN}` })).toBeVisible();

    await page.getByRole('button', { name: 'Delete' }).click();
    const dialog = page.getByRole('dialog', { name: 'Delete shipment' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Delete shipment' }).click();

    // Delete navigates back to the tracker; the deleted row is gone, the upload row stays.
    await expect(page.getByRole('heading', { name: 'Logistics tracker' })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: CHALLAN_MAIN })).toHaveCount(0);
    await expect(page.getByRole('row').filter({ hasText: CHALLAN_NEW })).toHaveCount(1);
  });
});
