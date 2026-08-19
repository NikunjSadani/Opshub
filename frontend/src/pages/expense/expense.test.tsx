import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { Upload } from './Upload';
import { Register } from './Register';
import { ReviewPanel } from './ReviewPanel';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Wrap a subtree with the app's providers. `initialEntries` drives the router. */
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

const INVOICE_ROW = {
  id: 1,
  status: 'CONFIRMED',
  needs_ocr: false,
  review_reasons: [],
  supplier_name: 'Acme Supplies Pvt Ltd',
  supplier_gstin: '27ABCDE1234F1Z5',
  buyer_name: 'Gifsy',
  buyer_gstin: '29AAAAA0000A1Z5',
  invoice_number: 'INV-2026-001',
  invoice_date: '2026-05-10',
  place_of_supply: 'Maharashtra',
  total_taxable_paise: 1000000,
  total_cgst_paise: 90000,
  total_sgst_paise: 90000,
  total_igst_paise: 0,
  round_off_paise: 0,
  grand_total_paise: 1180000,
  amount_in_words: 'Eleven thousand eight hundred rupees',
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Register', () => {
  it('renders invoices from GET /expense/invoices', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/expense/invoices')) return json([INVOICE_ROW]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<Register />);

    expect(await screen.findByText('Acme Supplies Pvt Ltd')).toBeInTheDocument();
    expect(screen.getByText('INV-2026-001')).toBeInTheDocument();
    expect(screen.getByText('27ABCDE1234F1Z5')).toBeInTheDocument();
    // Grand total is rendered from integer paise.
    expect(screen.getByText('₹11,800.00')).toBeInTheDocument();
    // The row's status badge (not the filter <option>) reads Confirmed.
    const row = screen.getByText('INV-2026-001').closest('tr') as HTMLElement;
    expect(within(row).getByText('Confirmed')).toBeInTheDocument();
  });
});

describe('Upload', () => {
  it('shows a per-file result list after upload', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/expense/invoices') && method === 'POST') {
          return json({
            batch_id: 7,
            invoice_count: 2,
            outcomes: [
              { file_id: 1, filename: 'a.pdf', invoice_id: 11, status: 'EXTRACTED', review_reasons: [] },
              {
                file_id: 2,
                filename: 'b.pdf',
                invoice_id: 12,
                status: 'NEEDS_REVIEW',
                review_reasons: ['grand total is low confidence'],
              },
            ],
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<Upload />);

    const input = document.getElementById('expense-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: {
        files: [
          new File(['x'], 'a.pdf', { type: 'application/pdf' }),
          new File(['y'], 'b.pdf', { type: 'application/pdf' }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: /upload 2 files/i }));

    expect(await screen.findByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('b.pdf')).toBeInTheDocument();
    expect(screen.getByText('Extracted')).toBeInTheDocument();
    expect(screen.getByText('Needs review')).toBeInTheDocument();
    expect(screen.getByText(/grand total is low confidence/i)).toBeInTheDocument();
  });

  it('surfaces the delete-and-re-upload dialog on a DUPLICATE and DELETEs then re-uploads on confirm', async () => {
    let deleted: string | null = null;
    let postCount = 0;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/expense/invoices') && method === 'POST') {
          postCount += 1;
          if (postCount === 1) {
            // First upload: the single file duplicates an existing invoice. The
            // summary scalars are FLAT and describe the EXISTING invoice;
            // `duplicate_of` is that stored invoice's id.
            return json({
              batch_id: 1,
              invoice_count: 0,
              outcomes: [
                {
                  file_id: 1,
                  filename: 'dup.pdf',
                  status: 'DUPLICATE',
                  duplicate_of: 99,
                  invoice_number: 'INV-OLD-9',
                  grand_total_paise: 250000,
                  review_reasons: [],
                },
              ],
            });
          }
          // Re-upload after the delete: now it extracts cleanly.
          return json({
            batch_id: 2,
            invoice_count: 1,
            outcomes: [
              { file_id: 2, filename: 'dup.pdf', invoice_id: 100, status: 'EXTRACTED', review_reasons: [] },
            ],
          });
        }
        if (url.match(/\/expense\/invoices\/99$/) && method === 'DELETE') {
          deleted = url;
          return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<Upload />);

    const input = document.getElementById('expense-files') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['z'], 'dup.pdf', { type: 'application/pdf' })] },
    });
    fireEvent.click(screen.getByRole('button', { name: /upload 1 file/i }));

    // The DUPLICATE row + its existing-invoice summary appear.
    expect(await screen.findByText('Duplicate')).toBeInTheDocument();
    expect(screen.getByText(/Matches existing invoice INV-OLD-9/i)).toBeInTheDocument();

    // Clicking "Resolve duplicate" opens the honest confirm dialog.
    fireEvent.click(screen.getByRole('button', { name: /resolve duplicate/i }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(
      screen.getByText(/An invoice with this number already exists/i),
    ).toBeInTheDocument();

    // Confirm → DELETE existing, then re-upload the same file.
    fireEvent.click(screen.getByRole('button', { name: /delete existing & re-upload/i }));

    await waitFor(() => expect(deleted).not.toBeNull());
    // The row updates to the fresh EXTRACTED outcome; the duplicate is gone.
    expect(await screen.findByText('Extracted')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText('Duplicate')).not.toBeInTheDocument(),
    );
    expect(postCount).toBe(2);
  });
});

describe('ReviewPanel', () => {
  /** An EXTRACTED invoice whose grand total is LOW_CONFIDENCE (needs resolving). */
  function detail(overrides: Record<string, unknown> = {}) {
    return {
      ...INVOICE_ROW,
      id: 5,
      status: 'NEEDS_REVIEW',
      review_reasons: ['grand total is low confidence'],
      fields: [
        { field_path: 'header.supplier_gstin', value_normalized: '27ABCDE1234F1Z5', value_raw: '27ABCDE1234F1Z5', confidence: 0.95, status: 'OK' },
        { field_path: 'header.invoice_number', value_normalized: 'INV-2026-001', value_raw: 'INV-2026-001', confidence: 0.95, status: 'OK' },
        { field_path: 'header.invoice_date', value_normalized: '2026-05-10', value_raw: '10-05-2026', confidence: 0.9, status: 'OK' },
        { field_path: 'totals.total_taxable_paise', value_normalized: '1000000', value_raw: '10,000.00', confidence: 0.9, status: 'OK' },
        { field_path: 'totals.grand_total_paise', value_normalized: null, value_raw: '1I800.00', confidence: 0.4, status: 'LOW_CONFIDENCE' },
      ],
      lines: [
        {
          line_no: 1,
          description: 'Widgets',
          hsn_sac: '8471',
          quantity: '10.000',
          unit: 'NOS',
          unit_rate_paise: 100000,
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

  it('makes a low-confidence field editable and gates Confirm until it is resolved', async () => {
    let patchBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/expense\/invoices\/5\/reviews$/) && method === 'PATCH') {
          patchBody = JSON.parse(String(init?.body));
          return json({ ...detail(), status: 'CONFIRMED' });
        }
        if (url.match(/\/expense\/invoices\/5$/)) return json(detail());
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/expense_invoice/invoices/:id" element={<ReviewPanel />} />
      </Routes>,
      ['/m/expense_invoice/invoices/5'],
    );

    // The low-confidence grand total renders an editable input; OK fields do not.
    const gtInput = (await screen.findByLabelText('Grand total')) as HTMLInputElement;
    expect(gtInput).toBeInTheDocument();
    expect(screen.queryByLabelText('Supplier GSTIN')).not.toBeInTheDocument();

    // Confirm is gated while the required grand total is unresolved.
    const confirm = screen.getByRole('button', { name: /confirm invoice/i });
    expect(confirm).toBeDisabled();

    // Typing a value resolves it → Confirm enables.
    fireEvent.change(gtInput, { target: { value: '11800.00' } });
    expect(confirm).toBeEnabled();

    // Confirm PATCHes the correction with confirm:true. The money field is edited
    // in RUPEES (₹11,800.00) but the wire `value` is integer PAISE (1180000) — the
    // backend coerces money with int(text), so sending "11800.00" would 400 and
    // sending "11800" would store ₹118 (the 100× bug this guards against).
    fireEvent.click(confirm);
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({
      corrections: [{ field_path: 'totals.grand_total_paise', value: '1180000' }],
      confirm: true,
    });
  });

  it('shows an existing money value in rupees, converts a rupee edit to paise, and blocks a malformed amount', async () => {
    let patchBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/expense\/invoices\/5\/reviews$/) && method === 'PATCH') {
          patchBody = JSON.parse(String(init?.body));
          return json({ ...detail(), status: 'CONFIRMED' });
        }
        if (url.match(/\/expense\/invoices\/5$/)) {
          // Grand total is LOW_CONFIDENCE but already has a normalized paise value
          // (250000 paise = ₹2,500.00) — the editable input must show rupees.
          return json(
            detail({
              fields: [
                { field_path: 'header.supplier_gstin', value_normalized: '27ABCDE1234F1Z5', value_raw: '27ABCDE1234F1Z5', confidence: 0.95, status: 'OK' },
                { field_path: 'header.invoice_number', value_normalized: 'INV-2026-001', value_raw: 'INV-2026-001', confidence: 0.95, status: 'OK' },
                { field_path: 'header.invoice_date', value_normalized: '2026-05-10', value_raw: '10-05-2026', confidence: 0.9, status: 'OK' },
                { field_path: 'totals.total_taxable_paise', value_normalized: '1000000', value_raw: '10,000.00', confidence: 0.9, status: 'OK' },
                { field_path: 'totals.grand_total_paise', value_normalized: '250000', value_raw: '2,500.00', confidence: 0.4, status: 'LOW_CONFIDENCE' },
              ],
            }),
          );
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/expense_invoice/invoices/:id" element={<ReviewPanel />} />
      </Routes>,
      ['/m/expense_invoice/invoices/5'],
    );

    const gtInput = (await screen.findByLabelText('Grand total')) as HTMLInputElement;
    // Existing 250000 paise renders in the input as rupees, not raw paise.
    expect(gtInput.value).toBe('2500.00');

    const confirm = screen.getByRole('button', { name: /confirm invoice/i });

    // A malformed amount blocks Confirm (backend would reject it).
    fireEvent.change(gtInput, { target: { value: '12.3.4' } });
    expect(confirm).toBeDisabled();

    // A valid rupee edit (with a thousands separator) → integer paise on the wire.
    fireEvent.change(gtInput, { target: { value: '1,234.56' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({
      corrections: [{ field_path: 'totals.grand_total_paise', value: '123456' }],
      confirm: true,
    });
  });
});
