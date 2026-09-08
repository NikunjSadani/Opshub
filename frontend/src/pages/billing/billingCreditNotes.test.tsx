import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { CreditNotesPage } from './CreditNotesPage';
import { CreditNoteUpload } from './CreditNoteUpload';
import { CreditNoteReview } from './CreditNoteReview';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` payload the mock provider fetches on sign-in, at a given billing level. */
function meResponse(level: 'VIEW' | 'OPERATE' | 'MANAGE' = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : `Billing ${level}`,
    is_administrator: level === 'MANAGE',
    module_levels: { billing: level },
    platform: level === 'MANAGE' ? ['iam', 'settings'] : [],
  });
}

function renderWithProviders(node: ReactNode, initialEntries: string[] = ['/']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <AuthProvider>
          <ToastProvider>{node}</ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Pick an option from a SearchableSelect combobox (Credited invoice is searchable):
 * focus to open the listbox, then mousedown the matching option (commits on mousedown).
 * The picker is disabled until its options query resolves — wait first. */
async function pickCombo(labelRe: RegExp, optionRe: RegExp) {
  const combo = screen.getByRole('combobox', { name: labelRe });
  await waitFor(() => expect(combo).toBeEnabled());
  fireEvent.focus(combo);
  const opt = await screen.findByRole('option', { name: optionRe });
  fireEvent.mouseDown(opt);
}

const CLIENTS = [
  { id: 1, name: 'Britannia', code: 'BRI', pan: null, credit_terms_days: null, active: true },
];

/** A CONFIRMED invoice row (backend list `InvoiceOut`) — the only kind a CN can credit. */
const CONFIRMED_INVOICE_ROW = {
  id: 50,
  status: 'CONFIRMED',
  needs_ocr: false,
  client_id: 1,
  po_id: 50,
  supplier_gstin: null,
  buyer_gstin: '29AAAAA0000A1Z5',
  invoice_number: 'CINV-2026-005',
  invoice_date: '2026-05-10',
  total_taxable_paise: 1000000,
  grand_total_paise: 1180000,
  created_at: '2026-05-10T00:00:00Z',
};

/** A register row (backend list `CNOut` projection). */
const CN_ROW = {
  id: 5,
  cn_number: 'CN-2026-005',
  client_id: 1,
  invoice_id: 50,
  invoice_number: 'CINV-2026-005',
  cn_date: '2026-05-20',
  grand_total_paise: 236000,
  status: 'NEEDS_MATCH',
};

/** The PO detail returned by GET /purchase-orders/50 — two OPEN lines to match against. */
const PO_DETAIL = {
  id: 50,
  po_number: 'PO-2026-050',
  client_id: '1',
  client_name: 'Britannia',
  project_id: '10',
  project_code: 'BRI-001',
  po_date: '2026-05-01',
  expected_procurement_date: null,
  status: 'IN_PROGRESS',
  line_count: 2,
  total_sell_paise: 5000000,
  created_at: '2026-05-01T00:00:00Z',
  client_gstin: null,
  notes: null,
  soft_copy_file: null,
  amendments_count: 0,
  lines: [
    {
      id: 501,
      product_id: '1',
      product_name: 'Biscuits carton',
      brand: null,
      model_number: null,
      description: 'Biscuits carton',
      uom: 'NOS',
      ordered_qty: '100',
      cost_price_paise: 8000,
      sell_price_paise: 10000,
      freight_paise: 0,
      packaging_paise: 0,
      handling_paise: 0,
      other_paise: 0,
      tax_rate: '18.00',
      line_status: 'OPEN',
      short_closed_qty: '0',
      short_close_reason: null,
    },
    {
      id: 502,
      product_id: '2',
      product_name: 'Display rack',
      brand: null,
      model_number: null,
      description: 'Display rack',
      uom: 'NOS',
      ordered_qty: '5',
      cost_price_paise: 40000,
      sell_price_paise: 50000,
      freight_paise: 0,
      packaging_paise: 0,
      handling_paise: 0,
      other_paise: 0,
      tax_rate: '18.00',
      line_status: 'OPEN',
      short_closed_qty: '0',
      short_close_reason: null,
    },
  ],
};

/**
 * A full credit-note detail (backend `CNDetail`) — NESTED: a `referenced_invoice`
 * object, a `source_file` object, and `lines` carrying `po_line_label` / `match_status`.
 * The referenced invoice is CONFIRMED, so confirm gates ONLY on the unmatched line.
 * `overrides` tailors it per test.
 */
function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    cn_number: 'CN-2026-005',
    client_id: 1,
    invoice_id: 50,
    cn_date: '2026-05-20',
    total_taxable_paise: 200000,
    total_cgst_paise: 18000,
    total_sgst_paise: 18000,
    total_igst_paise: 0,
    round_off_paise: 0,
    grand_total_paise: 236000,
    status: 'NEEDS_MATCH',
    needs_ocr: false,
    review_reasons: [],
    fields: [],
    reason: 'Short shipment',
    source_file: { id: 9, filename: 'cn-005.pdf' },
    referenced_invoice: {
      id: 50,
      invoice_number: 'CINV-2026-005',
      po_id: 50,
      grand_total_paise: 1180000,
      status: 'CONFIRMED',
    },
    lines: [
      {
        id: 71,
        line_no: 1,
        description: 'Biscuits carton',
        quantity: '20',
        taxable_paise: 200000,
        line_total_paise: 236000,
        po_line_item_id: null,
        po_line_label: null,
        match_status: 'UNMATCHED',
        // Null on an UNMATCHED line — no invoice line to compare against.
        billed_qty: null,
        already_credited_qty: null,
      },
    ],
    ...overrides,
  };
}

/** A MATCHED copy of the detail — the manual-match PATCH returns this. Not over-credited. */
function matchedDetail() {
  return detail({
    status: 'MATCHED',
    lines: [
      {
        id: 71,
        line_no: 1,
        description: 'Biscuits carton',
        quantity: '20',
        taxable_paise: 200000,
        line_total_paise: 236000,
        po_line_item_id: 501,
        po_line_label: 'Biscuits carton · qty 100 · ₹100.00',
        match_status: 'MANUAL',
        billed_qty: '100',
        already_credited_qty: '0',
      },
    ],
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Register (CreditNotesPage)', () => {
  it('renders CN rows with the referenced invoice number, credit total, and a status badge', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/billing/credit-notes')) return json([CN_ROW]);
        if (url.includes('/billing/invoices')) return json([CONFIRMED_INVOICE_ROW]);
        if (url.endsWith('/me')) return meResponse();
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/billing/credit-notes/*" element={<CreditNotesPage />} />
      </Routes>,
      ['/m/billing/credit-notes'],
    );

    // The row renders with its CN number, the referenced invoice number, total (paise→₹), status.
    expect(await screen.findByText('CN-2026-005')).toBeInTheDocument();
    const row = screen.getByText('CN-2026-005').closest('tr') as HTMLElement;
    expect(within(row).getByText('CINV-2026-005')).toBeInTheDocument();
    expect(within(row).getByText('₹2,360.00')).toBeInTheDocument();
    expect(within(row).getByText('Needs match')).toBeInTheDocument();
  });
});

describe('CreditNoteUpload', () => {
  it('keeps Upload disabled until an invoice + file are chosen, then surfaces every outcome', async () => {
    let postedForm: FormData | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/billing/credit-notes') && method === 'POST') {
          postedForm = init?.body as FormData;
          return json({
            outcomes: [
              { file_id: 1, status: 'NEEDS_MATCH', cn_id: 11, cn_number: 'CN-11' },
              { file_id: 2, status: 'DUPLICATE', cn_id: 99, cn_number: 'CN-OLD' },
            ],
          });
        }
        if (url.includes('/billing/invoices')) return json([CONFIRMED_INVOICE_ROW]);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<CreditNoteUpload />);

    const input = document.getElementById('cn-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: {
        files: [
          new File(['x'], 'good.pdf', { type: 'application/pdf' }),
          new File(['y'], 'dupe.pdf', { type: 'application/pdf' }),
        ],
      },
    });

    // Files chosen but no invoice → Upload stays disabled.
    const uploadBtn = screen.getByRole('button', { name: /upload 2 files/i });
    expect(uploadBtn).toBeDisabled();

    // Choosing the credited invoice enables it.
    await pickCombo(/credited invoice/i, /CINV-2026-005/i);
    expect(uploadBtn).toBeEnabled();

    fireEvent.click(uploadBtn);

    // Every outcome is surfaced — the duplicate row is NOT hidden; names come from the files.
    expect(await screen.findByText('good.pdf')).toBeInTheDocument();
    expect(screen.getByText('dupe.pdf')).toBeInTheDocument();
    expect(screen.getByText('Duplicate')).toBeInTheDocument();
    expect(screen.getByText('Needs match')).toBeInTheDocument();

    // The multipart body carried the required invoice_id and both files under `file`.
    expect(postedForm).not.toBeNull();
    const form = postedForm as unknown as FormData;
    expect(form.get('invoice_id')).toBe('50');
    expect(form.getAll('file')).toHaveLength(2);
  });

  it('a VIEW-only user cannot upload', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/billing/invoices')) return json([CONFIRMED_INVOICE_ROW]);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.endsWith('/me')) return meResponse('VIEW');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<CreditNoteUpload />);

    fireEvent.change(document.getElementById('cn-files') as HTMLInputElement, {
      target: { files: [new File(['x'], 'good.pdf', { type: 'application/pdf' })] },
    });
    await pickCombo(/credited invoice/i, /CINV-2026-005/i);

    // Even with an invoice + file, a viewer cannot upload — the button stays disabled.
    const uploadBtn = screen.getByRole('button', { name: /upload 1 file/i });
    await waitFor(() => expect(uploadBtn).toBeDisabled());
    expect(screen.getByText(/need Operate access/i)).toBeInTheDocument();
  });
});

describe('CreditNoteReview — detail + match', () => {
  function stub(handlers: {
    onDetail?: () => Response;
    onManualMatch?: (body: unknown) => Response;
    onConfirm?: (body: unknown) => Response;
    level?: 'VIEW' | 'OPERATE' | 'MANAGE';
    capture?: { manual?: (b: unknown) => void; confirm?: (b: unknown) => void };
  }) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/billing\/credit-notes\/5\/lines\/71\/match$/) && method === 'PATCH') {
          const body = JSON.parse(String(init?.body));
          handlers.capture?.manual?.(body);
          return (handlers.onManualMatch ?? (() => json(detail())))(body);
        }
        if (url.match(/\/billing\/credit-notes\/5\/review$/) && method === 'PATCH') {
          const body = JSON.parse(String(init?.body));
          handlers.capture?.confirm?.(body);
          return (handlers.onConfirm ?? (() => json({ ...detail(), status: 'CONFIRMED' })))(body);
        }
        if (url.match(/\/billing\/credit-notes\/5$/)) {
          return (handlers.onDetail ?? (() => json(detail())))();
        }
        if (url.match(/\/purchase-orders\/50$/)) return json(PO_DETAIL);
        if (url.endsWith('/me')) return meResponse(handlers.level ?? 'MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );
  }

  function renderReview() {
    renderWithProviders(
      <Routes>
        <Route path="/m/billing/credit-notes/:id" element={<CreditNoteReview />} />
      </Routes>,
      ['/m/billing/credit-notes/5'],
    );
  }

  it('shows the nested lines + referenced invoice, and maps an UNMATCHED line via the PATCH', async () => {
    const captured: unknown[] = [];
    stub({ capture: { manual: (b) => captured.push(b) } });

    renderReview();

    // The line renders from the NESTED shape with its UNMATCHED badge.
    expect(await screen.findByText('Unmatched')).toBeInTheDocument();
    // The referenced invoice (nested) is surfaced with its CONFIRMED status.
    expect(screen.getByText('CINV-2026-005')).toBeInTheDocument();
    expect(screen.getByText('CONFIRMED')).toBeInTheDocument();

    // The manual-map control lists the PO's OPEN lines; choosing one fires the PATCH.
    const select = await screen.findByLabelText(/Match line 1 to a PO line/i);
    fireEvent.change(select, { target: { value: '501' } });

    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ po_line_item_id: 501 });
  });

  it('gates Confirm while a line is UNMATCHED, then enables it once matched', async () => {
    stub({ onManualMatch: () => json(matchedDetail()) });

    renderReview();

    const confirm = await screen.findByRole('button', { name: /confirm credit note/i });
    // Blocked while the line is unmatched, with an honest reason.
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/Match every line to a PO line to confirm/i)).toBeInTheDocument();

    // Wait for the PO's OPEN lines to load, then map the line → PATCH returns MATCHED detail.
    await screen.findByRole('option', { name: /Biscuits carton/i });
    fireEvent.change(screen.getByLabelText(/Match line 1 to a PO line/i), {
      target: { value: '501' },
    });
    await screen.findByText('Manual');
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it('a VIEW-only user gets no Confirm/Cancel/Delete actions', async () => {
    stub({ level: 'VIEW' });
    renderReview();

    expect(await screen.findByText('Unmatched')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm credit note/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel credit note/i })).not.toBeInTheDocument();
  });

  it('an OPERATE user can confirm but gets no Cancel/Delete (MANAGE-only)', async () => {
    const captured: unknown[] = [];
    stub({
      level: 'OPERATE',
      onDetail: () => json(matchedDetail()),
      capture: { confirm: (b) => captured.push(b) },
    });

    renderReview();

    const confirm = await screen.findByRole('button', { name: /confirm credit note/i });
    expect(confirm).toBeEnabled();
    // MANAGE-only actions are absent for an operator.
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel credit note/i })).not.toBeInTheDocument();

    // Confirm PATCHes the review endpoint with confirm:true (no corrections).
    fireEvent.click(confirm);
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ confirm: true });
  });

  it('flags an over-credited matched line but leaves Confirm enabled (soft warning)', async () => {
    // A MATCHED line where already-credited (4) + this credit (2) = 6 exceeds the 4 the
    // invoice billed on the line. The tally is a SOFT flag — it must NOT gate Confirm.
    const overCredited = detail({
      status: 'MATCHED',
      lines: [
        {
          id: 71,
          line_no: 1,
          description: 'Biscuits carton',
          quantity: '2',
          taxable_paise: 200000,
          line_total_paise: 236000,
          po_line_item_id: 501,
          po_line_label: 'Biscuits carton · qty 100 · ₹100.00',
          match_status: 'MANUAL',
          billed_qty: '4',
          already_credited_qty: '4',
        },
      ],
    });
    stub({ level: 'OPERATE', onDetail: () => json(overCredited) });

    renderReview();

    // The honest over-credit warning renders on the line…
    expect(
      await screen.findByText(/this note credits 2 on this PO line — 6 credited in total vs 4 billed/i),
    ).toBeInTheDocument();
    // …and the invoice-billed context line names the other notes.
    expect(
      screen.getByText(/Invoice billed 4 · 4 already credited by other notes/i),
    ).toBeInTheDocument();

    // …but Confirm stays ENABLED — the backend soft-flags, never blocks.
    const confirm = await screen.findByRole('button', { name: /confirm credit note/i });
    expect(confirm).toBeEnabled();
  });

  it('warns on fan-in: two lines to the SAME PO line whose combined qty over-credits', async () => {
    // Each line credits 6 (≤ 10 billed alone → no per-line flag), but both map to PO line 501,
    // so the PO-line total 12 > 10 IS an over-credit. The warning must fire (the money audit's
    // finding: a per-line check would silently pass this).
    const fanIn = detail({
      status: 'MATCHED',
      lines: [
        {
          id: 81, line_no: 1, description: 'Cartons A', quantity: '6',
          taxable_paise: 600000, line_total_paise: 600000, po_line_item_id: 501,
          po_line_label: 'Cartons · qty 100', match_status: 'MANUAL',
          billed_qty: '10', already_credited_qty: '0',
        },
        {
          id: 82, line_no: 2, description: 'Cartons B', quantity: '6',
          taxable_paise: 600000, line_total_paise: 600000, po_line_item_id: 501,
          po_line_label: 'Cartons · qty 100', match_status: 'MANUAL',
          billed_qty: '10', already_credited_qty: '0',
        },
      ],
    });
    stub({ level: 'OPERATE', onDetail: () => json(fanIn) });
    renderReview();

    // Both rows warn with the PO-line total (12 credited vs 10 billed) — not a passing 6-vs-10.
    const warnings = await screen.findAllByText(
      /this note credits 12 on this PO line — 12 credited in total vs 10 billed/i,
    );
    expect(warnings.length).toBe(2);
  });

  it('renders an editable input for a MISSING field and Saves it as corrections keyed by `field`', async () => {
    const captured: unknown[] = [];
    const withMissing = detail({
      fields: [
        {
          field_path: 'total_taxable_paise',
          value_norm: null,
          value_raw: null,
          confidence: 0.2,
          source_engine: 'text',
          status: 'MISSING',
        },
      ],
    });
    stub({
      level: 'OPERATE',
      onDetail: () => json(withMissing),
      capture: { confirm: (b) => captured.push(b) },
    });

    renderReview();

    // The flagged field surfaces as an editable, labelled input (money → edited in ₹).
    const input = (await screen.findByLabelText(/Total taxable/i)) as HTMLInputElement;
    expect(input).toBeInTheDocument();

    // Correcting it reveals Save; the value is converted ₹100 → 10000 paise on the wire.
    fireEvent.change(input, { target: { value: '100' } });
    const save = await screen.findByRole('button', { name: /save corrections/i });
    fireEvent.click(save);

    await waitFor(() => expect(captured.length).toBe(1));
    // Frozen contract: array of { field, value } — the key is `field`, NOT `field_path`.
    expect(captured[0]).toEqual({
      corrections: [{ field: 'total_taxable_paise', value: '10000' }],
      confirm: false,
    });
  });

  it('renders the review-reasons and needs-OCR banners when present', async () => {
    stub({
      onDetail: () =>
        json(
          detail({
            needs_ocr: true,
            review_reasons: ['Credit total exceeds the invoice grand total', 'line 1 is not matched'],
          }),
        ),
    });

    renderReview();

    // The amber review-reasons list…
    expect(
      await screen.findByText(/Credit total exceeds the invoice grand total/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/line 1 is not matched/i)).toBeInTheDocument();
    // …and the needs-OCR banner.
    expect(screen.getByText(/no text layer/i)).toBeInTheDocument();
  });
});
