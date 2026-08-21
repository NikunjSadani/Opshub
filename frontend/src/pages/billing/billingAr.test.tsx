import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ReceivablesPage } from './ReceivablesPage';

type Level = 'VIEW' | 'OPERATE' | 'MANAGE';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` payload granting the given level on the `billing` module. */
function meResponse(level: Level = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'Billing ' + level,
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

const CLIENTS = [{ id: '10', name: 'Britannia', code: 'BRI', pan: null, credit_terms_days: null, active: true }];

/** An AR register row (backend InvoiceAROut). */
const AR_ROW = {
  invoice_id: 1,
  client_id: 10,
  invoice_number: 'INV-2026-001',
  invoice_date: '2026-04-01',
  due_date: '2026-05-01',
  grand_total_paise: 10000000, // ₹1,00,000.00
  credited_paise: 0,
  paid_paise: 7500000,
  applied_paise: 0,
  outstanding_paise: 2500000, // ₹25,000.00
  status: 'PART_PAID',
  overdue: true,
  aging_bucket: '31-60',
};

/** One invoice's AR detail (InvoiceARDetailOut). */
const AR_DETAIL = {
  ...AR_ROW,
  payments: [
    {
      id: 90,
      client_id: 10,
      invoice_id: 1,
      amount_paise: 7500000,
      received_on: '2026-04-20',
      mode: 'NEFT',
      reference: 'UTR123',
      note: null,
      created_at: '2026-04-20T00:00:00Z',
    },
  ],
  applied_advances: [
    { id: 700, advance_id: 501, invoice_id: 1, amount_paise: 0, created_at: '2026-04-22T00:00:00Z' },
  ],
};

const ADVANCES = [
  {
    advance_id: 501,
    client_id: 10,
    po_id: null,
    amount_paise: 5000000,
    applied_paise: 0,
    remaining_paise: 5000000,
    received_on: '2026-03-01',
    mode: 'NEFT',
    reference: 'ADV-A',
    note: null,
  },
  {
    advance_id: 502,
    client_id: 10,
    po_id: null,
    amount_paise: 2000000,
    applied_paise: 0,
    remaining_paise: 2000000,
    received_on: '2026-03-15',
    mode: 'UPI',
    reference: 'ADV-B',
    note: null,
  },
];

/** FIFO suggestion (SuggestionOut) — proposes ₹500 from the oldest advance (501). */
const SUGGESTION = [
  {
    advance_id: 501,
    amount_paise: 50000, // ₹500.00
    advance_remaining_paise: 5000000,
    received_on: '2026-03-01',
    reference: 'ADV-A',
  },
];

/** Change a <select> by locating one of its options (avoids label ambiguity). */
async function selectByOption(optionText: RegExp, value: string) {
  const opt = await screen.findByRole('option', { name: optionText });
  const select = opt.closest('select') as HTMLSelectElement;
  fireEvent.change(select, { target: { value } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AR register', () => {
  it('renders AR rows with rupees, status and aging', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/billing/ar')) return json([AR_ROW]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />);

    expect(await screen.findByText('INV-2026-001')).toBeInTheDocument();
    const row = screen.getByText('INV-2026-001').closest('tr') as HTMLElement;
    // outstanding_paise 2500000 → ₹25,000.00; grand_total 10000000 → ₹1,00,000.00
    expect(within(row).getByText('₹25,000.00')).toBeInTheDocument();
    expect(within(row).getByText('₹1,00,000.00')).toBeInTheDocument();
    // status + overdue + aging
    expect(within(row).getByText('Part-paid')).toBeInTheDocument();
    expect(within(row).getByText('Overdue')).toBeInTheDocument();
    expect(within(row).getByText('31-60 days')).toBeInTheDocument();
    // client name is joined from the clients list (AR rows carry only client_id)
    expect(within(row).getByText(/BRI — Britannia/)).toBeInTheDocument();

    // Filtering by status re-queries with a status param.
    await selectByOption(/^Paid$/, 'PAID');
    await waitFor(() =>
      expect(urls.some((u) => u.includes('/billing/ar') && u.includes('status=PAID'))).toBe(true),
    );
  });
});

describe('Record payment', () => {
  it('posts the payment converting ₹→paise', async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/billing/advances')) return json(ADVANCES);
        if (url.includes('/billing/payments') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ id: 91, ...AR_ROW, amount_paise: 25000 }, 201);
        }
        if (/\/billing\/invoices\/1\/ar$/.test(url)) return json(AR_DETAIL);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />, ['/1']);

    fireEvent.click(await screen.findByRole('button', { name: /record payment/i }));
    const dialog = await screen.findByRole('dialog');
    // Amount pre-fills to the outstanding (₹25,000.00 → "25000.00"); override to ₹250.
    const amount = within(dialog).getByLabelText(/amount/i) as HTMLInputElement;
    expect(amount.value).toBe('25000.00');
    fireEvent.change(amount, { target: { value: '250' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /record payment/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const body = postedBody as unknown as { invoice_id: number; amount_paise: number };
    expect(body.invoice_id).toBe(1);
    // ₹250 → 25000 paise
    expect(body.amount_paise).toBe(25000);
  });

  it('surfaces the server over-outstanding 422 honestly', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/billing/advances')) return json(ADVANCES);
        if (url.includes('/billing/payments') && method === 'POST') {
          return json({ detail: 'payment exceeds invoice outstanding' }, 422);
        }
        if (/\/billing\/invoices\/1\/ar$/.test(url)) return json(AR_DETAIL);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />, ['/1']);

    fireEvent.click(await screen.findByRole('button', { name: /record payment/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/amount/i), { target: { value: '9999999' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /record payment/i }));

    // The 422 detail is shown in the dialog, not swallowed.
    expect(await within(dialog).findByText(/exceeds invoice outstanding/i)).toBeInTheDocument();
  });
});

describe('Apply advance', () => {
  it('pre-fills the FIFO suggestion but lets the operator override advance + amount', async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/advance-suggestion')) return json(SUGGESTION);
        if (url.includes('/billing/advance-applications') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ id: 800, advance_id: 502, invoice_id: 1, amount_paise: 30000, created_at: '2026-05-01T00:00:00Z' }, 201);
        }
        if (url.includes('/billing/advances')) return json(ADVANCES);
        if (/\/billing\/invoices\/1\/ar$/.test(url)) return json(AR_DETAIL);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />, ['/1']);

    fireEvent.click(await screen.findByRole('button', { name: /apply advance/i }));
    const dialog = await screen.findByRole('dialog');

    // Suggestion pre-fills: advance 501 selected, amount ₹500.00.
    const amount = within(dialog).getByLabelText(/amount/i) as HTMLInputElement;
    await waitFor(() => expect(amount.value).toBe('500.00'));
    const advanceSelect = within(dialog).getByRole('combobox') as HTMLSelectElement;
    expect(advanceSelect.value).toBe('501');

    // Operator overrides both: advance 502, amount ₹300.
    fireEvent.change(advanceSelect, { target: { value: '502' } });
    fireEvent.change(amount, { target: { value: '300' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /apply advance/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const body = postedBody as unknown as { advance_id: number; invoice_id: number; amount_paise: number };
    expect(body.advance_id).toBe(502);
    expect(body.invoice_id).toBe(1);
    // ₹300 → 30000 paise
    expect(body.amount_paise).toBe(30000);
  });
});

describe('RBAC', () => {
  it('a VIEW user sees the tracker read-only (no record/apply controls)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('VIEW');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/billing/ar')) return json([AR_ROW]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />);

    // The tracker still renders for a viewer…
    expect(await screen.findByText('INV-2026-001')).toBeInTheDocument();
    // …but there are no write controls anywhere on it.
    expect(screen.queryByRole('button', { name: /record payment/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /record advance/i })).not.toBeInTheDocument();
  });

  it('a VIEW user on the invoice detail sees no Record / Apply / Un-apply controls', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('VIEW');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/billing/advances')) return json(ADVANCES);
        if (/\/billing\/invoices\/1\/ar$/.test(url)) return json(AR_DETAIL);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ReceivablesPage />, ['/1']);

    // Detail renders (payments table visible)…
    expect(await screen.findByText('Payments')).toBeInTheDocument();
    // …with every write control gated out.
    expect(screen.queryByRole('button', { name: /record payment/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /apply advance/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /un-apply/i })).not.toBeInTheDocument();
  });
});
