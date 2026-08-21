import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { InvoiceUpload } from './InvoiceUpload';
import { InvoiceReview } from './InvoiceReview';
import { InvoicesPage } from './InvoicesPage';

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

const CLIENTS = [
  { id: 1, name: 'Britannia', code: 'BRI', pan: null, credit_terms_days: null, active: true },
];

const PURCHASE_ORDERS = [
  {
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
  },
];

/** A register row (backend list `InvoiceOut` projection). */
const INVOICE_ROW = {
  id: 5,
  status: 'NEEDS_MATCH',
  needs_ocr: false,
  client_id: 1,
  po_id: 50,
  supplier_gstin: '27ZZZZZ0000Z1Z5',
  buyer_gstin: '29AAAAA0000A1Z5',
  invoice_number: 'CINV-2026-005',
  invoice_date: '2026-05-10',
  total_taxable_paise: 1000000,
  grand_total_paise: 1180000,
  created_at: '2026-05-10T00:00:00Z',
};

/** The PO detail returned by GET /purchase-orders/50 — two OPEN lines to match against. */
const PO_DETAIL = {
  ...PURCHASE_ORDERS[0],
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
 * A full invoice detail (backend `InvoiceDetailOut`). One clean required field set,
 * one UNMATCHED line. `overrides` tailors it per test.
 */
function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    batch_id: 1,
    status: 'NEEDS_MATCH',
    needs_ocr: false,
    review_reasons: ['line 1 is not matched to a PO line'],
    client_id: 1,
    po_id: 50,
    source_file_id: 9,
    supplier_gstin: '27ZZZZZ0000Z1Z5',
    buyer_gstin: '29AAAAA0000A1Z5',
    invoice_number: 'CINV-2026-005',
    invoice_date: '2026-05-10',
    due_date: null,
    total_taxable_paise: 1000000,
    total_cgst_paise: 90000,
    total_sgst_paise: 90000,
    total_igst_paise: 0,
    round_off_paise: 0,
    grand_total_paise: 1180000,
    confirmed_by: null,
    confirmed_at: null,
    fields: [
      { field_path: 'header.buyer_gstin', value_norm: '29AAAAA0000A1Z5', value_raw: '29AAAAA0000A1Z5', confidence: 0.95, source_engine: 'text', status: 'OK' },
      { field_path: 'header.invoice_number', value_norm: 'CINV-2026-005', value_raw: 'CINV-2026-005', confidence: 0.95, source_engine: 'text', status: 'OK' },
      { field_path: 'header.invoice_date', value_norm: '2026-05-10', value_raw: '10-05-2026', confidence: 0.9, source_engine: 'text', status: 'OK' },
      { field_path: 'totals.total_taxable_paise', value_norm: '1000000', value_raw: '10,000.00', confidence: 0.9, source_engine: 'text', status: 'OK' },
      { field_path: 'totals.grand_total_paise', value_norm: '1180000', value_raw: '11,800.00', confidence: 0.9, source_engine: 'text', status: 'OK' },
    ],
    lines: [
      {
        id: 71,
        line_no: 1,
        po_line_item_id: null,
        match_status: 'UNMATCHED',
        description: 'Biscuits carton',
        hsn_sac: '1905',
        quantity: '100',
        unit: 'NOS',
        unit_rate_paise: 10000,
        taxable_paise: 1000000,
        gst_rate: '18.00',
        cgst_paise: 90000,
        sgst_paise: 90000,
        igst_paise: 0,
        line_total_paise: 1180000,
      },
    ],
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Register (InvoicesPage)', () => {
  it('renders invoices from GET /billing/invoices and filters by status', async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/billing/invoices')) {
          seen.push(url);
          return json([INVOICE_ROW]);
        }
        if (url.includes('/purchase-orders')) return json(PURCHASE_ORDERS);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.endsWith('/me')) return meResponse();
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/billing/*" element={<InvoicesPage />} />
      </Routes>,
      ['/m/billing'],
    );

    // The row renders with its invoice number, mapped client + PO, total (paise→₹), status.
    expect(await screen.findByText('CINV-2026-005')).toBeInTheDocument();
    const row = screen.getByText('CINV-2026-005').closest('tr') as HTMLElement;
    expect(within(row).getByText('BRI — Britannia')).toBeInTheDocument();
    expect(within(row).getByText('PO-2026-050')).toBeInTheDocument();
    expect(within(row).getByText('₹11,800.00')).toBeInTheDocument();
    expect(within(row).getByText('Needs match')).toBeInTheDocument();

    // Choosing a status filter re-queries with ?status=CONFIRMED (debounced).
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'CONFIRMED' } });
    await waitFor(() => expect(seen.some((u) => u.includes('status=CONFIRMED'))).toBe(true));
  });
});

describe('InvoiceUpload', () => {
  it('keeps Upload disabled until a client + file are chosen, then surfaces a rejected/duplicate outcome', async () => {
    let postedForm: FormData | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/billing/invoices') && method === 'POST') {
          postedForm = init?.body as FormData;
          return json({
            batch_id: 7,
            invoice_count: 1,
            outcomes: [
              { file_id: 1, filename: 'good.pdf', invoice_id: 11, status: 'NEEDS_MATCH', duplicate_of: null, buyer_gstin: null, invoice_number: 'CINV-11', grand_total_paise: 500000, review_reasons: [], message: null },
              { file_id: 2, filename: 'dupe.pdf', invoice_id: null, status: 'DUPLICATE', duplicate_of: 99, buyer_gstin: null, invoice_number: 'CINV-OLD', grand_total_paise: 250000, review_reasons: [], message: null },
              { file_id: 3, filename: 'bad.pdf', invoice_id: null, status: 'REJECTED', duplicate_of: null, buyer_gstin: null, invoice_number: null, grand_total_paise: null, review_reasons: [], message: 'the document could not be read' },
            ],
          });
        }
        if (url.includes('/purchase-orders')) return json(PURCHASE_ORDERS);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<InvoiceUpload />);

    await screen.findByRole('option', { name: /BRI — Britannia/i });

    const input = document.getElementById('billing-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'good.pdf', { type: 'application/pdf' })] },
    });

    // File chosen but no client → Upload stays disabled.
    const uploadBtn = screen.getByRole('button', { name: /upload 1 file/i });
    expect(uploadBtn).toBeDisabled();

    // Choosing the client enables it.
    fireEvent.change(screen.getByLabelText(/Client/i), { target: { value: '1' } });
    expect(uploadBtn).toBeEnabled();

    fireEvent.click(uploadBtn);

    // Every outcome is surfaced — the rejected and duplicate rows are NOT hidden.
    expect(await screen.findByText('good.pdf')).toBeInTheDocument();
    expect(screen.getByText('Duplicate')).toBeInTheDocument();
    expect(screen.getByText('Rejected')).toBeInTheDocument();
    expect(screen.getByText(/Matches existing invoice CINV-OLD/i)).toBeInTheDocument();
    expect(screen.getByText(/the document could not be read/i)).toBeInTheDocument();

    // The multipart body carried the required client_id.
    expect(postedForm).not.toBeNull();
    expect((postedForm as unknown as FormData).get('client_id')).toBe('1');
  });
});

describe('InvoiceReview — detail + match', () => {
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
        if (url.match(/\/billing\/invoices\/5\/lines\/71\/match$/) && method === 'PATCH') {
          const body = JSON.parse(String(init?.body));
          handlers.capture?.manual?.(body);
          return (handlers.onManualMatch ?? (() => json(detail())))(body);
        }
        if (url.match(/\/billing\/invoices\/5\/review$/) && method === 'PATCH') {
          const body = JSON.parse(String(init?.body));
          handlers.capture?.confirm?.(body);
          return (handlers.onConfirm ?? (() => json({ ...detail(), status: 'CONFIRMED' })))(body);
        }
        if (url.match(/\/billing\/invoices\/5$/)) return (handlers.onDetail ?? (() => json(detail())))();
        if (url.match(/\/purchase-orders\/50$/)) return json(PO_DETAIL);
        if (url.endsWith('/me')) return meResponse(handlers.level ?? 'MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );
  }

  function renderReview() {
    renderWithProviders(
      <Routes>
        <Route path="/m/billing/:id" element={<InvoiceReview />} />
      </Routes>,
      ['/m/billing/5'],
    );
  }

  it('shows lines with a match badge and maps an UNMATCHED line via the PATCH', async () => {
    const captured: unknown[] = [];
    stub({ capture: { manual: (b) => captured.push(b) } });

    renderReview();

    // The line renders with its UNMATCHED badge.
    expect(await screen.findByText('Unmatched')).toBeInTheDocument();

    // The manual-map control lists the PO's OPEN lines; choosing one fires the PATCH
    // with the chosen po_line_item_id.
    const select = await screen.findByLabelText(/Match line 1 to a PO line/i);
    fireEvent.change(select, { target: { value: '501' } });

    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ po_line_item_id: 501 });
  });

  it('gates Confirm while a line is UNMATCHED, then enables it once matched', async () => {
    // The manual-match PATCH returns the now-MATCHED detail; the mutation seeds it into
    // the detail cache, so Confirm flips from disabled to enabled.
    const matched = detail({
      status: 'MATCHED',
      review_reasons: [],
      lines: [{ ...detail().lines[0], po_line_item_id: 501, match_status: 'MANUAL' }],
    });
    stub({ onManualMatch: () => json(matched) });

    renderReview();

    const confirm = await screen.findByRole('button', { name: /confirm invoice/i });
    // Blocked while the line is unmatched, with an honest reason.
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/Match every line to a PO line to confirm/i)).toBeInTheDocument();

    // Wait for the PO's OPEN lines to load into the match control before choosing one.
    await screen.findByRole('option', { name: /Biscuits carton/i });
    // Map the line → the PATCH returns the MATCHED detail → Confirm enables.
    fireEvent.change(screen.getByLabelText(/Match line 1 to a PO line/i), {
      target: { value: '501' },
    });
    await screen.findByText('Manual');
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it('a VIEW-only user gets no Confirm/Cancel/Delete actions', async () => {
    stub({ level: 'VIEW' });
    renderReview();

    // The screen still renders (VIEW can read the detail + lines).
    expect(await screen.findByText('Unmatched')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm invoice/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel invoice/i })).not.toBeInTheDocument();
  });

  it('an OPERATE user can confirm but gets no Cancel/Delete (MANAGE-only)', async () => {
    const captured: unknown[] = [];
    stub({
      level: 'OPERATE',
      onDetail: () =>
        json(detail({
          status: 'MATCHED',
          review_reasons: [],
          lines: [{ ...detail().lines[0], po_line_item_id: 501, match_status: 'MATCHED' }],
        })),
      capture: { confirm: (b) => captured.push(b) },
    });

    renderReview();

    const confirm = await screen.findByRole('button', { name: /confirm invoice/i });
    expect(confirm).toBeEnabled();
    // MANAGE-only actions are absent for an operator.
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /cancel invoice/i })).not.toBeInTheDocument();

    // Confirm PATCHes the review endpoint with confirm:true.
    fireEvent.click(confirm);
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ corrections: [], confirm: true });
  });
});
