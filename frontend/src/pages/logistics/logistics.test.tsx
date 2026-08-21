import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { LogisticsModule } from './LogisticsModule';
import { ShipmentDetail } from './ShipmentDetail';
import { ShipmentUpload } from './ShipmentUpload';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` payload; grants the given level on the `logistics` module. */
function meResponse(level: 'VIEW' | 'OPERATE' | 'MANAGE' = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'Logistics Viewer',
    is_administrator: level === 'MANAGE',
    module_levels: { logistics: level },
    platform: level === 'MANAGE' ? ['iam', 'settings'] : [],
  });
}

/** Wrap a subtree with the app's providers. `initialEntries` drives the router.
 * Returns the QueryClient too so a test can force a refetch (external-change re-sync). */
function renderWithProviders(node: ReactNode, initialEntries: string[] = ['/']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <AuthProvider>
          <ToastProvider>{node}</ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, ...utils };
}

/** The resolved challan block the backend nests on a shipment. */
const CHALLAN_INFO = {
  invoice_number: 'INV-2026-042',
  po_number: 'PO-2026-011',
  project_code: 'BRI-001',
  consignee_name: 'Acme Stores',
  consignee_gstin: '27AAAAA0000A1Z5',
  consignee_address: '12 MG Road, Pune',
};

/** A register/summary row with the resolved challan surfaced as a NESTED object
 * (matching the real backend ShipmentOut shape, not the old flat challan_* fields). */
const SHIPMENT_ROW = {
  id: '5',
  challan_number: 'GIF/DC/26-27/L/000189',
  status: 'DELIVERED',
  delivery_partner: 'BlueDart',
  tracking_id: 'BD-77123',
  consignee_name: 'Acme Stores',
  dispatched_on: '2026-08-10',
  delivered_on: '2026-08-12',
  challan: CHALLAN_INFO,
};

/** Full detail (summary + delivery/consignee fields + POD). */
const SHIPMENT_DETAIL = {
  ...SHIPMENT_ROW,
  status: 'PENDING',
  address: '12 MG Road, Pune',
  phone: '9800011122',
  pincode: '411001',
  notes: 'Leave at reception',
  pod_file: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Logistics tracker register', () => {
  it('renders shipment rows with a status badge and the resolved invoice# / PO#', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/logistics/shipments')) return json([SHIPMENT_ROW]);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/*" element={<LogisticsModule />} />
      </Routes>,
      ['/m/logistics'],
    );

    const cell = await screen.findByText('GIF/DC/26-27/L/000189');
    const row = cell.closest('tr') as HTMLElement;
    // The status badge (not the filter option) reads Delivered.
    expect(within(row).getByText('Delivered')).toBeInTheDocument();
    // The resolved challan info (nested `challan` object) renders in the row.
    expect(within(row).getByText('INV-2026-042')).toBeInTheDocument();
    expect(within(row).getByText('PO-2026-011')).toBeInTheDocument();
    expect(within(row).getByText('BlueDart')).toBeInTheDocument();
    // With a single row and no cap hit, the honest count reads "Showing 1".
    expect(screen.getByText('Showing 1')).toBeInTheDocument();
    // An OPERATE/MANAGE user sees the write entry points.
    expect(screen.getByRole('link', { name: /new shipment/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /upload \.xlsx/i })).toBeInTheDocument();
  });

  it('drives the query from the status filter', async () => {
    let lastListUrl = '';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/logistics/shipments')) {
          lastListUrl = url;
          return json([]);
        }
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/*" element={<LogisticsModule />} />
      </Routes>,
      ['/m/logistics'],
    );

    // Wait for the first (unfiltered) list load.
    await waitFor(() => expect(lastListUrl).toContain('/logistics/shipments'));
    // The register requests a full page (limit ceiling), not the silent default of 50.
    expect(lastListUrl).toContain('limit=200');

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'DELIVERED' } });

    // The debounced filter settles and refires the query with the status param.
    await waitFor(() => expect(lastListUrl).toContain('status=DELIVERED'));
  });
});

describe('Logistics RBAC gating', () => {
  it('hides the New/Upload actions from a VIEW-only user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/logistics/shipments')) return json([SHIPMENT_ROW]);
        if (url.endsWith('/me')) return meResponse('VIEW');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/*" element={<LogisticsModule />} />
      </Routes>,
      ['/m/logistics'],
    );

    // The register still renders for a viewer...
    expect(await screen.findByText('GIF/DC/26-27/L/000189')).toBeInTheDocument();
    // ...but the OPERATE-gated write entry points do not.
    expect(screen.queryByRole('link', { name: /new shipment/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /upload \.xlsx/i })).not.toBeInTheDocument();
  });

  it('hides Delete + the status update control from a VIEW-only user on the detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/logistics\/shipments\/5$/)) return json(SHIPMENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('VIEW');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/:id" element={<ShipmentDetail />} />
      </Routes>,
      ['/m/logistics/5'],
    );

    // The detail loads read-only.
    await screen.findByText('Shipment GIF/DC/26-27/L/000189');
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByText('Update status')).not.toBeInTheDocument();
  });
});

describe('Shipment status update', () => {
  it('PATCHes the chosen status', async () => {
    let patchBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/logistics\/shipments\/5$/) && method === 'PATCH') {
          patchBody = JSON.parse(String(init?.body));
          return json({ ...SHIPMENT_DETAIL, status: 'DELIVERED' });
        }
        if (url.match(/\/logistics\/shipments\/5$/)) return json(SHIPMENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/:id" element={<ShipmentDetail />} />
      </Routes>,
      ['/m/logistics/5'],
    );

    const select = (await screen.findByLabelText('Delivery status')) as HTMLSelectElement;
    // Update is disabled until the status actually changes.
    const updateBtn = screen.getByRole('button', { name: /^update$/i });
    expect(updateBtn).toBeDisabled();

    fireEvent.change(select, { target: { value: 'DELIVERED' } });
    expect(updateBtn).toBeEnabled();

    fireEvent.click(updateBtn);
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({ status: 'DELIVERED' });
  });
});

describe('Shipment bulk upload', () => {
  it('shows the created/updated counts and never hides row errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/logistics/shipments/upload') && method === 'POST') {
          return json({
            created: 2,
            updated: 1,
            errors: [{ row: 4, message: 'challan number could not be resolved' }],
          });
        }
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ShipmentUpload />);

    const input = document.getElementById('logistics-file') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'dump.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })] },
    });

    fireEvent.click(screen.getByRole('button', { name: /^upload$/i }));

    // The summary counts render...
    expect(await screen.findByText('Created: 2')).toBeInTheDocument();
    expect(screen.getByText('Updated: 1')).toBeInTheDocument();
    expect(screen.getByText('Row errors: 1')).toBeInTheDocument();
    // ...and the errored row's `message` text (not the old `reason`) is shown in full.
    expect(screen.getByText(/challan number could not be resolved/i)).toBeInTheDocument();
    expect(screen.getByText(/Row 4/)).toBeInTheDocument();
  });
});

describe('Logistics register honesty + a11y + re-sync', () => {
  it('warns instead of implying completeness when the page cap is hit', async () => {
    // Exactly SHIPMENTS_PAGE_LIMIT (200) rows come back -> the list may be truncated.
    const rows = Array.from({ length: 200 }, (_, i) => ({
      ...SHIPMENT_ROW,
      id: String(i + 1),
      challan_number: `GIF/DC/26-27/L/${String(i + 1).padStart(6, '0')}`,
    }));
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/logistics/shipments')) return json(rows);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/*" element={<LogisticsModule />} />
      </Routes>,
      ['/m/logistics'],
    );

    // The honest cap notice renders; the misleading "Showing 200" does not.
    expect(
      await screen.findByText(/Showing first 200 — refine filters to narrow the list\./),
    ).toBeInTheDocument();
    expect(screen.queryByText('Showing 200')).not.toBeInTheDocument();
  });

  it('gives the detail POD file input an accessible label', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/logistics\/shipments\/5$/)) return json(SHIPMENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/logistics/:id" element={<ShipmentDetail />} />
      </Routes>,
      ['/m/logistics/5'],
    );

    await screen.findByText('Shipment GIF/DC/26-27/L/000189');
    expect(screen.getByLabelText('Proof of delivery file')).toBeInTheDocument();
  });

  it('re-syncs the status control when the server status changes on refetch', async () => {
    // First GET -> PENDING; every later GET (the forced refetch) -> DELIVERED.
    let getCount = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/logistics\/shipments\/5$/) && method === 'GET') {
          getCount += 1;
          return json({ ...SHIPMENT_DETAIL, status: getCount === 1 ? 'PENDING' : 'DELIVERED' });
        }
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    const { qc } = renderWithProviders(
      <Routes>
        <Route path="/m/logistics/:id" element={<ShipmentDetail />} />
      </Routes>,
      ['/m/logistics/5'],
    );

    const select = (await screen.findByLabelText('Delivery status')) as HTMLSelectElement;
    expect(select.value).toBe('PENDING');

    // Simulate an external change landing via a refetch.
    await qc.invalidateQueries({ queryKey: ['logistics', 'detail'] });
    await waitFor(() => expect(select.value).toBe('DELIVERED'));
  });
});
