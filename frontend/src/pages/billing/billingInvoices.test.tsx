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

/** Pick an option from a SearchableSelect combobox (Client / Purchase order / Project are
 * searchable): focus to open the listbox, then mousedown the matching option (commits on
 * mousedown). The picker is disabled until its options query resolves — wait first. */
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

/** ACTIVE projects for the selected client (backend `ProjectOut`; ids are ints on the wire). */
const PROJECTS = [
  {
    id: 10,
    code: 'BRI-001',
    client_id: '1',
    client_code: 'BRI',
    client_name: 'Britannia',
    name: 'Q2 Activation',
    start_date: null,
    status: 'ACTIVE',
    description: null,
    created_at: '2026-04-01T00:00:00Z',
  },
  {
    id: 11,
    code: 'BRI-002',
    client_id: '1',
    client_code: 'BRI',
    client_name: 'Britannia',
    name: 'Diwali Campaign',
    start_date: null,
    status: 'ACTIVE',
    description: null,
    created_at: '2026-04-02T00:00:00Z',
  },
];

/** A register row (backend list `InvoiceOut` projection). */
const INVOICE_ROW = {
  id: 5,
  status: 'NEEDS_MATCH',
  needs_ocr: false,
  client_id: 1,
  po_id: 50,
  project_id: null,
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
    project_id: null,
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
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<InvoiceUpload />);

    const input = document.getElementById('billing-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'good.pdf', { type: 'application/pdf' })] },
    });

    // File chosen but no client → Upload stays disabled.
    const uploadBtn = screen.getByRole('button', { name: /upload 1 file/i });
    expect(uploadBtn).toBeDisabled();

    // Choosing the client enables it.
    await pickCombo(/client/i, /BRI — Britannia/i);
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

  it('surfaces the duplicate list (not a bare error) when the WHOLE batch is a 409', async () => {
    // When every file is a duplicate the backend returns 409 — but its body still carries
    // the per-file outcomes. The screen must show the friendly "matched an existing invoice"
    // list (with a link to the original), not a raw "Upload failed: 409".
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/billing/invoices') && method === 'POST') {
          return json(
            {
              batch_id: 8,
              invoice_count: 1,
              outcomes: [
                { file_id: 1, filename: 'dupe.pdf', invoice_id: null, status: 'DUPLICATE', duplicate_of: 99, buyer_gstin: null, invoice_number: 'CINV-OLD', grand_total_paise: 250000, review_reasons: [], message: null },
              ],
            },
            409,
          );
        }
        if (url.includes('/purchase-orders')) return json(PURCHASE_ORDERS);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<InvoiceUpload />);
    const input = document.getElementById('billing-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'dupe.pdf', { type: 'application/pdf' })] },
    });
    await pickCombo(/client/i, /BRI — Britannia/i);
    fireEvent.click(screen.getByRole('button', { name: /upload 1 file/i }));

    // The duplicate row + the existing-invoice link render — the 409 was handled as a result.
    expect(await screen.findByText('dupe.pdf')).toBeInTheDocument();
    expect(screen.getByText('Duplicate')).toBeInTheDocument();
    expect(screen.getByText(/Matches existing invoice CINV-OLD/i)).toBeInTheDocument();
  });

  it('with a client and no PO, shows the ACTIVE-project picker and sends the chosen project_id', async () => {
    let postedForm: FormData | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/billing/invoices') && method === 'POST') {
          postedForm = init?.body as FormData;
          return json({
            batch_id: 8,
            invoice_count: 1,
            outcomes: [
              { file_id: 1, filename: 'standalone.pdf', invoice_id: 21, status: 'NEEDS_REVIEW', duplicate_of: null, buyer_gstin: null, invoice_number: 'CINV-21', grand_total_paise: 300000, review_reasons: [], message: null },
            ],
          });
        }
        if (url.includes('/purchase-orders')) return json([]); // no POs → the PO-less path
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<InvoiceUpload />);

    // Before a client is chosen, there is no Project picker.
    expect(screen.queryByLabelText(/^Project/i)).not.toBeInTheDocument();

    // Choosing the client reveals the Project picker with that client's ACTIVE projects.
    await pickCombo(/client/i, /BRI — Britannia/i);
    await screen.findByLabelText(/^Project/i);
    await pickCombo(/^Project/i, /BRI-001 — Q2 Activation/i);

    // Choose a file and upload.
    const input = document.getElementById('billing-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'standalone.pdf', { type: 'application/pdf' })] },
    });
    fireEvent.click(screen.getByRole('button', { name: /upload 1 file/i }));

    await screen.findByText('standalone.pdf');
    expect(postedForm).not.toBeNull();
    expect((postedForm as unknown as FormData).get('client_id')).toBe('1');
    // The chosen project rides the multipart form.
    expect((postedForm as unknown as FormData).get('project_id')).toBe('10');
  });

  it('hides the Project picker once a PO is selected (the PO carries the project)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/purchase-orders')) return json(PURCHASE_ORDERS);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<InvoiceUpload />);

    await pickCombo(/client/i, /BRI — Britannia/i);
    // No PO chosen yet → the Project picker is present.
    expect(await screen.findByLabelText(/^Project/i)).toBeInTheDocument();

    // Select a PO; its project wins, so the Project picker hides.
    await pickCombo(/purchase order/i, /PO-2026-050/i);
    await waitFor(() =>
      expect(screen.queryByLabelText(/^Project/i)).not.toBeInTheDocument(),
    );
  });
});

describe('InvoiceReview — detail + match', () => {
  function stub(handlers: {
    onDetail?: () => Response;
    onManualMatch?: (body: unknown) => Response;
    onConfirm?: (body: unknown) => Response;
    onSetProject?: (body: { project_id: number | null }) => Response;
    level?: 'VIEW' | 'OPERATE' | 'MANAGE';
    capture?: {
      manual?: (b: unknown) => void;
      confirm?: (b: unknown) => void;
      project?: (b: unknown) => void;
    };
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
        if (url.match(/\/billing\/invoices\/5\/project$/) && method === 'PATCH') {
          const body = JSON.parse(String(init?.body)) as { project_id: number | null };
          handlers.capture?.project?.(body);
          return (
            handlers.onSetProject ??
            (() => json(detail({ po_id: null, project_id: body.project_id })))
          )(body);
        }
        if (url.match(/\/billing\/invoices\/5$/)) return (handlers.onDetail ?? (() => json(detail())))();
        if (url.match(/\/purchase-orders\/50$/)) return json(PO_DETAIL);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
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

  it('a PO-less invoice shows the standalone note + an ENABLED Confirm, and confirms', async () => {
    // No PO linked: the backend derives status MATCHED once the required fields are in
    // order, and confirm must not be gated on line-matching (there is nothing to match).
    const captured: unknown[] = [];
    stub({
      level: 'OPERATE',
      onDetail: () =>
        json(
          detail({
            po_id: null,
            status: 'MATCHED',
            review_reasons: [],
            lines: [{ ...detail().lines[0], po_line_item_id: null, match_status: 'UNMATCHED' }],
          }),
        ),
      capture: { confirm: (b) => captured.push(b) },
    });

    renderReview();

    // The honest standalone-AR note replaces the match controls…
    expect(await screen.findByText(/No PO linked/i)).toBeInTheDocument();
    expect(
      screen.getByText(/This invoice will be confirmed as a standalone AR record/i),
    ).toBeInTheDocument();
    // …and there is no per-line "Match line … to a PO line" control.
    expect(screen.queryByLabelText(/Match line 1 to a PO line/i)).not.toBeInTheDocument();

    // Confirm is enabled despite the unmatched line, and PATCHes with confirm:true.
    const confirm = screen.getByRole('button', { name: /confirm invoice/i });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ corrections: [], confirm: true });
  });

  it('a PO-linked invoice with an unmatched line still shows the match UI and blocks confirm', async () => {
    stub({ level: 'OPERATE' });
    renderReview();

    // The match control is present (PO-linked flow unchanged)…
    expect(await screen.findByLabelText(/Match line 1 to a PO line/i)).toBeInTheDocument();
    expect(screen.queryByText(/No PO linked/i)).not.toBeInTheDocument();

    // …and Confirm stays blocked with the honest reason.
    const confirm = screen.getByRole('button', { name: /confirm invoice/i });
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/Match every line to a PO line to confirm/i)).toBeInTheDocument();
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

  it('a PO-less editable invoice shows the Project control and PATCHes the chosen project', async () => {
    const captured: unknown[] = [];
    stub({
      level: 'OPERATE',
      onDetail: () =>
        json(
          detail({
            po_id: null,
            project_id: null,
            status: 'MATCHED',
            review_reasons: [],
            lines: [{ ...detail().lines[0], po_line_item_id: null, match_status: 'UNMATCHED' }],
          }),
        ),
      capture: { project: (b) => captured.push(b) },
    });

    renderReview();

    // The standalone note renders, and (on the no-PO path) the editable Project control.
    expect(await screen.findByText(/No PO linked/i)).toBeInTheDocument();
    const select = await screen.findByLabelText('Project attribution');
    await screen.findByRole('option', { name: /BRI-001 — Q2 Activation/i });

    // Choosing a project PATCHes /billing/invoices/5/project with the numeric { project_id }.
    fireEvent.change(select, { target: { value: '10' } });
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0]).toEqual({ project_id: 10 });
  });

  it('a CONFIRMED PO-less invoice shows its assigned project read-only (no select)', async () => {
    stub({
      level: 'OPERATE',
      onDetail: () =>
        json(
          detail({
            po_id: null,
            project_id: 10,
            status: 'CONFIRMED',
            confirmed_at: '2026-05-11T00:00:00Z',
            review_reasons: [],
          }),
        ),
    });

    renderReview();

    expect(await screen.findByText(/No PO linked/i)).toBeInTheDocument();
    // The resolved project name shows read-only; there is no editable Project select.
    expect(await screen.findByText(/BRI-001 — Q2 Activation/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Project attribution')).not.toBeInTheDocument();
  });

  it('a PO-linked invoice does not show the Project attribution control', async () => {
    stub({ level: 'OPERATE' }); // default detail is PO-linked (po_id 50)
    renderReview();

    // The PO match UI is present; the standalone note + project control are not.
    expect(await screen.findByLabelText(/Match line 1 to a PO line/i)).toBeInTheDocument();
    expect(screen.queryByText(/No PO linked/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Project attribution')).not.toBeInTheDocument();
  });
});

describe('InvoiceReview — manual line editor', () => {
  function renderReview() {
    renderWithProviders(
      <Routes>
        <Route path="/m/billing/:id" element={<InvoiceReview />} />
      </Routes>,
      ['/m/billing/5'],
    );
  }

  /** The default extracted line (backend `LineOut`), the one seeded by `detail()`. */
  const LINE_71 = detail().lines[0];

  it('adds a line to a lineless (EXTRACTED) invoice via the header button and renders the returned detail', async () => {
    const posted: { url: string; body: Record<string, unknown> }[] = [];
    const addedLine = {
      id: 90,
      line_no: 1,
      po_line_item_id: null,
      match_status: 'UNMATCHED',
      description: 'New widget',
      hsn_sac: null,
      quantity: '3',
      unit: null,
      unit_rate_paise: 10050,
      taxable_paise: null,
      gst_rate: null,
      cgst_paise: null,
      sgst_paise: null,
      igst_paise: null,
      line_total_paise: null,
    };
    // A lineless standalone (no PO) invoice in an editable status → the in-table empty
    // state, with the Add button living in the section header.
    const lineless = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [] });
    const withLine = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [addedLine] });

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/billing\/invoices\/5\/lines$/) && method === 'POST') {
          posted.push({ url, body: JSON.parse(String(init?.body)) });
          return json(withLine);
        }
        if (url.match(/\/billing\/invoices\/5$/)) return json(lineless);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderReview();

    // Lineless → the table shows its empty state; the Add button is in the header (outside it).
    expect(await screen.findByText('No line items')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    // Fill the modal: description + a decimal quantity + a rupee unit-rate.
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/description/i), {
      target: { value: 'New widget' },
    });
    fireEvent.change(within(dialog).getByLabelText(/quantity/i), { target: { value: '3' } });
    fireEvent.change(within(dialog).getByLabelText(/unit rate/i), { target: { value: '100.50' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /add line/i }));

    // The POST carried ONLY the set keys, with ₹→paise conversion (100.50 → 10050).
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].body).toEqual({
      description: 'New widget',
      quantity: '3',
      unit_rate_paise: 10050,
    });
    // The returned detail (now carrying the line) renders it.
    expect(await screen.findByText('New widget')).toBeInTheDocument();
  });

  it('keeps the add-line submit disabled until a non-empty description is entered', async () => {
    const lineless = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [] });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/billing\/invoices\/5$/)) return json(lineless);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderReview();

    expect(await screen.findByText('No line items')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    const dialog = screen.getByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: /add line/i });
    // Blocked with an empty description…
    expect(submit).toBeDisabled();
    // …and enabled once a description is typed.
    fireEvent.change(within(dialog).getByLabelText(/description/i), {
      target: { value: 'Widget' },
    });
    expect(submit).toBeEnabled();
  });

  it('blocks add-line submit on an invalid quantity or GST rate with an inline error', async () => {
    const lineless = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [] });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/billing\/invoices\/5$/)) return json(lineless);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderReview();
    expect(await screen.findByText('No line items')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /add line/i }));

    const dialog = screen.getByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: /add line/i });
    // A valid description alone enables submit…
    fireEvent.change(within(dialog).getByLabelText(/description/i), { target: { value: 'Widget' } });
    expect(submit).toBeEnabled();
    // …a bad GST rate ("18%") blocks it inline (not an opaque server 422)…
    fireEvent.change(within(dialog).getByLabelText(/GST rate/i), { target: { value: '18%' } });
    expect(within(dialog).getByText(/GST rate from 0 to 100/i)).toBeInTheDocument();
    expect(submit).toBeDisabled();
    // …fixing it re-enables, and a bad quantity ("ten") blocks again inline.
    fireEvent.change(within(dialog).getByLabelText(/GST rate/i), { target: { value: '18' } });
    expect(submit).toBeEnabled();
    fireEvent.change(within(dialog).getByLabelText(/quantity/i), { target: { value: 'ten' } });
    expect(within(dialog).getByText(/a number with up to 3 decimals/i)).toBeInTheDocument();
    expect(submit).toBeDisabled();
  });

  it('deletes a line via the confirm dialog and drops the row', async () => {
    const deleted: string[] = [];
    const oneLine = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [LINE_71] });
    const noLines = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [] });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/billing\/invoices\/5\/lines\/71$/) && method === 'DELETE') {
          deleted.push(url);
          return json(noLines);
        }
        if (url.match(/\/billing\/invoices\/5$/)) return json(oneLine);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderReview();

    expect(await screen.findByText('Biscuits carton')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /delete line 1/i }));

    // Confirm the deletion in the dialog.
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/Delete line 1\?/i)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: /delete line/i }));

    // The DELETE fired and the returned (now-empty) detail drops the row.
    await waitFor(() => expect(deleted.length).toBe(1));
    await waitFor(() =>
      expect(screen.queryByText('Biscuits carton')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('No line items')).toBeInTheDocument();
  });

  it('edits a line: pre-fills from the line and PATCHes the converted body', async () => {
    const patched: Record<string, unknown>[] = [];
    const oneLine = detail({ po_id: null, status: 'EXTRACTED', review_reasons: [], lines: [LINE_71] });
    const edited = detail({
      po_id: null,
      status: 'EXTRACTED',
      review_reasons: [],
      lines: [{ ...LINE_71, description: 'Biscuits carton XL' }],
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/billing\/invoices\/5\/lines\/71$/) && method === 'PATCH') {
          patched.push(JSON.parse(String(init?.body)));
          return json(edited);
        }
        if (url.match(/\/billing\/invoices\/5$/)) return json(oneLine);
        if (url.match(/\/projects(\?|$)/)) return json(PROJECTS);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderReview();

    expect(await screen.findByText('Biscuits carton')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /edit line 1/i }));

    // The form pre-fills from the line (paise→₹ on money).
    const dialog = screen.getByRole('dialog');
    const desc = within(dialog).getByLabelText(/description/i) as HTMLInputElement;
    expect(desc.value).toBe('Biscuits carton');
    fireEvent.change(desc, { target: { value: 'Biscuits carton XL' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save line/i }));

    // The PATCH body carries the edited description + the round-tripped ₹→paise money.
    await waitFor(() => expect(patched.length).toBe(1));
    expect(patched[0]).toEqual({
      description: 'Biscuits carton XL',
      hsn_sac: '1905',
      quantity: '100',
      unit: 'NOS',
      gst_rate: '18.00',
      unit_rate_paise: 10000,
      taxable_paise: 1000000,
      cgst_paise: 90000,
      sgst_paise: 90000,
      igst_paise: 0,
      line_total_paise: 1180000,
    });
    expect(await screen.findByText('Biscuits carton XL')).toBeInTheDocument();
  });
});
