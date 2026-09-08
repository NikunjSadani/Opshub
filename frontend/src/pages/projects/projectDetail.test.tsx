import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ProjectDetail } from './ProjectDetail';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` granting VIEW on projects (reads only need VIEW). */
function meResponse(): Response {
  return json({
    id: 1,
    email: 'viewer@example.com',
    name: 'Vic Viewer',
    role_id: 2,
    role_name: 'Projects Viewer',
    is_administrator: false,
    module_levels: { projects: 'VIEW' },
    platform: [],
  });
}

const PROJECT = {
  id: 11,
  code: 'BRI-001',
  client_id: 7,
  client_code: 'BRI',
  client_name: 'Britannia Industries',
  name: 'Q3 Trade Scheme',
  start_date: '2026-07-01',
  status: 'ACTIVE' as const,
  description: 'Trade rewards for Q3.',
  created_at: '2026-07-01T00:00:00Z',
};

const PO_ROW = {
  id: 1,
  po_number: 'PO-2026-001',
  client_id: 7,
  client_name: 'Britannia Industries',
  project_id: 11,
  project_code: 'BRI-001',
  po_date: '2026-05-10',
  expected_procurement_date: null,
  status: 'CONFIRMED' as const,
  line_count: 3,
  total_sell_paise: null,
  total_client_sell_paise: 2500000,
  total_client_freight_paise: 0,
  total_client_extras_paise: 0,
  agency_fee_type: 'NONE' as const,
  agency_fee_percent: null,
  agency_fee_amount_paise: null,
  agency_fee_computed_paise: 0,
  // ₹25,000.00 client-facing order value.
  total_with_agency_paise: 2500000,
  created_at: '2026-05-10T00:00:00Z',
};

/** Render ProjectDetail at /m/projects/11 with the app providers. */
function renderDetail(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/m/projects/11']}>
        <AuthProvider>
          <ToastProvider>
            <Routes>
              <Route path="/m/projects/:id" element={node} />
            </Routes>
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ProjectDetail', () => {
  it('renders the project header and a table of its purchase orders with a working View link', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // The scoped PO list — the project id must be forwarded as a query param.
        if (url.match(/\/purchase-orders\?/) && url.includes('project_id=11')) {
          return json([PO_ROW]);
        }
        if (url.match(/\/projects\/11$/)) return json(PROJECT);
        if (url.endsWith('/me')) return meResponse();
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDetail(<ProjectDetail />);

    // Header: project code + name + client.
    expect(await screen.findByText('BRI-001')).toBeInTheDocument();
    expect(screen.getByText('Q3 Trade Scheme')).toBeInTheDocument();
    expect(screen.getByText('Britannia Industries')).toBeInTheDocument();

    // The PO row renders with its number + client-facing order value.
    const poCell = await screen.findByText('PO-2026-001');
    const row = poCell.closest('tr') as HTMLElement;
    expect(within(row).getByText('₹25,000.00')).toBeInTheDocument();
    expect(within(row).getByText('3')).toBeInTheDocument();

    // The View link drills through to the PO detail in the Sales Orders module.
    const view = within(row).getByRole('link', { name: /view/i });
    expect(view.getAttribute('href')).toBe('/m/sales_orders/1');

    // The scoped list requests the whole bounded set (backend max), not the register's
    // default first page — so a project's POs are never silently truncated to 50.
    const poCall = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.includes('/purchase-orders?'));
    expect(poCall).toContain('limit=200');
  });

  it('shows the empty state when the project has no purchase orders', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/purchase-orders\?/) && url.includes('project_id=11')) {
          return json([]);
        }
        if (url.match(/\/projects\/11$/)) return json(PROJECT);
        if (url.endsWith('/me')) return meResponse();
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDetail(<ProjectDetail />);

    expect(await screen.findByText('BRI-001')).toBeInTheDocument();
    expect(
      await screen.findByText('No purchase orders for this project yet.'),
    ).toBeInTheDocument();
  });
});
