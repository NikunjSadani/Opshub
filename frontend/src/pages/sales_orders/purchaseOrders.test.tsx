import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { PurchaseOrdersPage } from './PurchaseOrdersPage';
import { POForm } from './POForm';
import { POUpload } from './POUpload';
import { PODetail } from './PODetail';

type Level = 'VIEW' | 'OPERATE' | 'MANAGE';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` payload granting the given level on the `sales_orders` module. */
function meResponse(level: Level = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'PO ' + level,
    is_administrator: level === 'MANAGE',
    module_levels: { sales_orders: level },
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

const CLIENTS = [{ id: '10', name: 'Britannia', code: 'BRI', active: true }];
const PROJECTS = [
  {
    id: '20',
    code: 'BRI-001',
    client_id: '10',
    client_code: 'BRI',
    client_name: 'Britannia',
    name: 'Q3 Trade Rewards',
    start_date: null,
    status: 'ACTIVE',
    description: null,
    created_at: '2026-08-01T00:00:00Z',
  },
];
const PRODUCTS = [
  { id: '5', code: 'P1', name: 'Widget', brand: 'Acme', model_number: null, uom: 'PCS' },
];
const CLIENT_DETAIL = {
  id: '10',
  name: 'Britannia',
  code: 'BRI',
  gstins: [{ id: '7', gstin: '27ABCDE1234F1Z5', legal_name: 'Britannia', is_default: true }],
};

const PO_ROW = {
  id: '1',
  po_number: 'PO-2026-001',
  client_id: '10',
  client_name: 'Britannia',
  project_id: '20',
  project_code: 'BRI-001',
  po_date: '2026-05-10',
  expected_procurement_date: null,
  status: 'DRAFT',
  line_count: 2,
  total_sell_paise: 2500000,
  created_at: '2026-05-10T00:00:00Z',
};

const PO_DETAIL = {
  ...PO_ROW,
  client_gstin: '27ABCDE1234F1Z5',
  notes: 'Handle with care',
  soft_copy_file: null,
  amendments_count: 0,
  lines: [
    {
      id: '101',
      product_id: '5',
      product_name: 'Widget',
      brand: 'Acme',
      model_number: null,
      description: 'Blue widget',
      uom: 'PCS',
      ordered_qty: '10.000',
      cost_price_paise: 100000,
      sell_price_paise: 125000,
      freight_paise: 0,
      packaging_paise: 0,
      handling_paise: 0,
      other_paise: 0,
      tax_rate: '18.00',
      line_status: 'OPEN',
      short_closed_qty: '0.000',
      short_close_reason: null,
    },
  ],
};

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

describe('PO Register', () => {
  it('renders PO rows (money from paise) and filters by status', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/purchase-orders')) return json([PO_ROW]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<PurchaseOrdersPage />);

    expect(await screen.findByText('PO-2026-001')).toBeInTheDocument();
    // total_sell_paise (2500000) renders in rupees.
    expect(screen.getByText('₹25,000.00')).toBeInTheDocument();
    const row = screen.getByText('PO-2026-001').closest('tr') as HTMLElement;
    expect(within(row).getByText('Draft')).toBeInTheDocument();

    // Filtering by status re-queries the list with a status param.
    await selectByOption(/^Cancelled$/, 'CANCELLED');
    await waitFor(() =>
      expect(urls.some((u) => u.includes('/purchase-orders') && u.includes('status=CANCELLED'))).toBe(
        true,
      ),
    );
  });
});

describe('PO create form', () => {
  it('submits a PO with one line, converting rupee inputs to integer paise', async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/products')) return json(PRODUCTS);
        if (url.includes('/purchase-orders') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ ...PO_DETAIL }, 201);
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<POForm />);

    // PO number.
    fireEvent.change(await screen.findByLabelText(/po number/i), {
      target: { value: 'PO-NEW-1' },
    });
    // Client → enables the project picker.
    await selectByOption(/BRI — Britannia/, '10');
    await selectByOption(/BRI-001 — Q3 Trade Rewards/, '20');
    // PO date (required).
    fireEvent.change(screen.getByLabelText(/^po date/i), { target: { value: '2026-05-10' } });
    // Line: product + qty + cost/sell in rupees.
    await selectByOption(/Widget/, '5');
    fireEvent.change(screen.getByLabelText(/ordered qty/i), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/cost price/i), { target: { value: '100.50' } });
    fireEvent.change(screen.getByLabelText(/sell price/i), { target: { value: '250' } });

    const submit = screen.getByRole('button', { name: /create purchase order/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(postedBody).not.toBeNull());
    const body = postedBody as unknown as {
      po_number: string;
      client_id: string;
      project_id: string;
      lines: Array<{
        product_id: string;
        ordered_qty: string;
        cost_price_paise: number;
        sell_price_paise: number;
      }>;
    };
    expect(body.po_number).toBe('PO-NEW-1');
    expect(body.client_id).toBe('10');
    expect(body.project_id).toBe('20');
    // ₹100.50 → 10050 paise; ₹250 → 25000 paise.
    expect(body.lines[0].cost_price_paise).toBe(10050);
    expect(body.lines[0].sell_price_paise).toBe(25000);
    expect(body.lines[0].product_id).toBe('5');
  });
});

describe('PO RBAC gating', () => {
  function stubList(level: Level) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse(level);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/purchase-orders')) return json([PO_ROW]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  }

  function stubDetail(level: Level) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse(level);
        if (/\/purchase-orders\/\d+$/.test(url)) return json(PO_DETAIL);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  }

  it('hides the New PO button from a VIEW-only user', async () => {
    stubList('VIEW');
    renderWithProviders(<PurchaseOrdersPage />);
    expect(await screen.findByText('PO-2026-001')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /new po/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument();
  });

  it('shows New PO to an OPERATE user', async () => {
    stubList('OPERATE');
    renderWithProviders(<PurchaseOrdersPage />);
    expect(await screen.findByRole('button', { name: /new po/i })).toBeInTheDocument();
  });

  it('on detail, an OPERATE user can Amend but not Void; a MANAGE user can Void', async () => {
    stubDetail('OPERATE');
    const { unmount } = renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );
    expect(await screen.findByRole('button', { name: /amend/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^void$/i })).not.toBeInTheDocument();
    unmount();

    stubDetail('MANAGE');
    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );
    expect(await screen.findByRole('button', { name: /^void$/i })).toBeInTheDocument();
  });
});

describe('PO void flow', () => {
  it('a MANAGE user voids with a required reason', async () => {
    let voidBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (/\/purchase-orders\/\d+\/void$/.test(url) && method === 'POST') {
          voidBody = JSON.parse(String(init?.body));
          return json({ ...PO_DETAIL, status: 'CANCELLED' });
        }
        if (/\/purchase-orders\/\d+$/.test(url)) return json(PO_DETAIL);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    fireEvent.click(await screen.findByRole('button', { name: /^void$/i }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText(/reason/i), {
      target: { value: 'Cancelled by client' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: /void purchase order/i }));

    await waitFor(() => expect(voidBody).not.toBeNull());
    expect(voidBody).toEqual({ reason: 'Cancelled by client' });
  });
});

describe('PO bulk upload', () => {
  it('shows the created / skipped / errored summary and never hides errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/purchase-orders/upload') && method === 'POST') {
          return json(
            {
              created: ['PO-1'],
              skipped: [{ po_number: 'PO-2', reason: 'po_number already exists' }],
              errors: [{ row: 5, reason: 'unknown product code XYZ' }],
            },
            201,
          );
        }
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<POUpload />);

    await selectByOption(/BRI — Britannia/, '10');
    await selectByOption(/BRI-001 — Q3 Trade Rewards/, '20');

    const fileInput = document.getElementById('po-file') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(['x'], 'pos.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });

    const uploadBtn = screen.getByRole('button', { name: /^upload$/i });
    await waitFor(() => expect(uploadBtn).toBeEnabled());
    fireEvent.click(uploadBtn);

    // Every bucket is surfaced — created, skipped, AND errors (with reasons).
    expect(await screen.findByText('PO-1')).toBeInTheDocument();
    expect(screen.getByText(/po_number already exists/i)).toBeInTheDocument();
    expect(screen.getByText(/unknown product code XYZ/i)).toBeInTheDocument();
    expect(screen.getByText(/Row 5/i)).toBeInTheDocument();
  });
});
