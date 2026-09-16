import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

/** `GET /me` granting VIEW on projects (reads only need VIEW). No sales_orders access,
 * so the "Products" section stays hidden and never fetches `/project-products`. */
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

/** `GET /me` granting projects VIEW + sales_orders OPERATE — sees the Products section
 * and may tag/remove/edit pricing (backend gates `product.tag` at OPERATE). No platform
 * IAM → NOT an admin, so the admin-only Actual sell/freight inputs stay hidden. */
function meResponseOperate(): Response {
  return json({
    id: 1,
    email: 'op@example.com',
    name: 'Olga Operator',
    role_id: 3,
    role_name: 'Sales Operator',
    is_administrator: false,
    module_levels: { projects: 'VIEW', sales_orders: 'OPERATE' },
    platform: [],
  });
}

/** `GET /me` like {@link meResponseOperate} but WITH platform IAM → an admin, so the
 * Actual sell/freight pricing inputs are visible. */
function meResponseAdmin(): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: 'Administrator',
    is_administrator: true,
    module_levels: { projects: 'VIEW', sales_orders: 'OPERATE' },
    platform: ['iam', 'settings'],
  });
}

/** A tagged product WITH a pricing template (ProjectProductOut — keyed by product_id).
 * client_sell 25000 (₹250.00), Our CP 9000 (₹90.00), tax "18.00". */
const PP_A = {
  product_id: 21,
  code: 'P-A',
  name: 'Alpha Widget',
  brand: 'Acme',
  model_number: null,
  category: 'Widgets',
  active: true,
  description: null,
  uom: null,
  cost_price_paise: 9000,
  original_cost_price_paise: null,
  client_sell_price_paise: 25000,
  vendor_sell_price_paise: null,
  sell_price_paise: null,
  client_freight_paise: null,
  vendor_freight_paise: null,
  freight_paise: null,
  packaging_paise: null,
  handling_paise: null,
  other_paise: null,
  tax_rate: '18.00',
};
/** A second tagged product with NO pricing yet (all template fields null). */
const PP_B = {
  product_id: 22,
  code: 'P-B',
  name: 'Beta Gadget',
  brand: 'Beta',
  model_number: null,
  category: 'Gadgets',
  active: true,
  description: null,
  uom: null,
  cost_price_paise: null,
  original_cost_price_paise: null,
  client_sell_price_paise: null,
  vendor_sell_price_paise: null,
  sell_price_paise: null,
  client_freight_paise: null,
  vendor_freight_paise: null,
  freight_paise: null,
  packaging_paise: null,
  handling_paise: null,
  other_paise: null,
  tax_rate: null,
};
/** The full catalogue as the Add picker's `/products` search returns it (PickerProduct). */
const CAT_A = { id: '21', code: 'P-A', name: 'Alpha Widget', brand: 'Acme', model_number: null, uom: 'PCS' };
const CAT_B = { id: '22', code: 'P-B', name: 'Beta Gadget', brand: 'Beta', model_number: null, uom: 'PCS' };

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

  it('hides the Products section for a user without sales_orders access', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/purchase-orders\?/) && url.includes('project_id=11')) return json([]);
        if (url.match(/\/projects\/11$/)) return json(PROJECT);
        if (url.endsWith('/me')) return meResponse();
        // A `/project-products` call here would mean we failed to hide the section.
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDetail(<ProjectDetail />);

    expect(await screen.findByText('BRI-001')).toBeInTheDocument();
    // Purchase orders heading is present; the Tagged products section is not.
    expect(screen.getByText('Purchase orders')).toBeInTheDocument();
    expect(screen.queryByText('Tagged products')).not.toBeInTheDocument();
  });

  it('renders tagged products and lets an OPERATE user tag then remove one', async () => {
    // The project's tagged set, mutated by POST/DELETE so a refetch reflects the change.
    const tagged: Array<typeof PP_A | typeof PP_B> = [PP_A];
    const ppCatalogue = [PP_A, PP_B];
    const catalogue = [CAT_A, CAT_B];

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.endsWith('/me')) return meResponseOperate();
      if (url.match(/\/projects\/11$/)) return json(PROJECT);
      if (url.match(/\/purchase-orders\?/)) return json([]);
      // Order matters: match `/project-products` BEFORE `/products` (substring overlap).
      if (url.match(/\/project-products\?/) && method === 'GET') return json(tagged.slice());
      if (url.match(/\/project-products$/) && method === 'POST') {
        const body = JSON.parse(String(init?.body)) as { project_id: number; product_id: number };
        const prod = ppCatalogue.find((p) => p.product_id === body.product_id) ?? PP_B;
        if (!tagged.some((t) => t.product_id === prod.product_id)) tagged.push(prod);
        return json(prod, 201);
      }
      const del = url.match(/\/project-products\/11\/(\d+)$/);
      if (del && method === 'DELETE') {
        const pid = Number(del[1]);
        const idx = tagged.findIndex((t) => t.product_id === pid);
        if (idx >= 0) tagged.splice(idx, 1);
        return json({ deleted: true });
      }
      // The add-picker's full-catalogue search (no project scope).
      if (url.match(/\/products\?/) && method === 'GET') return json(catalogue.slice());
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderDetail(<ProjectDetail />);

    // The section renders with the already-tagged product + its template Client sell.
    expect(await screen.findByText('Tagged products')).toBeInTheDocument();
    expect(await screen.findByText('Alpha Widget')).toBeInTheDocument();
    expect(screen.getByText('₹250.00')).toBeInTheDocument(); // client_sell_price_paise 25000

    // Tag Beta Gadget via the picker: focus opens the list, mousedown commits.
    const combo = screen.getByRole('combobox', { name: /add product/i });
    fireEvent.focus(combo);
    const option = await screen.findByText('P-B — Beta Gadget (Beta)');
    fireEvent.mouseDown(option);

    // POST body carries numeric project_id + product_id (the fixed wire contract).
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (c) => String(c[0]).endsWith('/project-products') && (c[1]?.method ?? 'GET') === 'POST',
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({ project_id: 11, product_id: 22 });
    });

    // After invalidation + refetch, the newly tagged product appears in the table.
    expect(await screen.findByText('Beta Gadget')).toBeInTheDocument();

    // Remove Alpha Widget: its row's Remove button DELETEs, then it disappears.
    const alphaRow = screen.getByText('Alpha Widget').closest('tr') as HTMLElement;
    fireEvent.click(within(alphaRow).getByRole('button', { name: /remove alpha widget/i }));

    await waitFor(() => {
      const delCall = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).match(/\/project-products\/11\/21$/) &&
          (c[1]?.method ?? 'GET') === 'DELETE',
      );
      expect(delCall).toBeTruthy();
    });
    await waitFor(() => expect(screen.queryByText('Alpha Widget')).not.toBeInTheDocument());
  });

  it('the Edit modal PATCHes the entered pricing as an integer-paise body', async () => {
    let patchUrl: string | null = null;
    let patchBody: Record<string, unknown> | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.endsWith('/me')) return meResponseOperate();
      if (url.match(/\/projects\/11$/)) return json(PROJECT);
      if (url.match(/\/purchase-orders\?/)) return json([]);
      if (url.match(/\/project-products\?/) && method === 'GET') return json([PP_A]);
      const patch = url.match(/\/project-products\/11\/(\d+)$/);
      if (patch && method === 'PATCH') {
        patchUrl = url;
        patchBody = JSON.parse(String(init?.body));
        return json({ ...PP_A, cost_price_paise: 12050, client_sell_price_paise: 30000 });
      }
      if (url.match(/\/products\?/) && method === 'GET') return json([CAT_A, CAT_B]);
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderDetail(<ProjectDetail />);

    // Open the Edit modal on the Alpha row.
    const alphaRow = (await screen.findByText('Alpha Widget')).closest('tr') as HTMLElement;
    fireEvent.click(within(alphaRow).getByRole('button', { name: /edit alpha widget/i }));
    const dialog = await screen.findByRole('dialog');

    // Pre-filled from the template (paise → ₹): Our CP 9000 → 90.00, Client sell 25000 → 250.00.
    expect((within(dialog).getByLabelText(/our cp/i) as HTMLInputElement).value).toBe('90.00');
    expect((within(dialog).getByLabelText(/client sell/i) as HTMLInputElement).value).toBe('250.00');
    // A non-admin never sees the admin-only actual sell/freight inputs.
    expect(within(dialog).queryByLabelText(/actual sell/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/actual freight/i)).not.toBeInTheDocument();

    // Change Our CP → ₹120.50 and Client sell → ₹300; save.
    fireEvent.change(within(dialog).getByLabelText(/our cp/i), { target: { value: '120.50' } });
    fireEvent.change(within(dialog).getByLabelText(/client sell/i), { target: { value: '300' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save pricing/i }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchUrl).toMatch(/\/project-products\/11\/21$/);
    // Money is integer paise; tax carried as a trimmed string; blanks omitted.
    expect(patchBody).toMatchObject({
      cost_price_paise: 12050,
      client_sell_price_paise: 30000,
      tax_rate: '18.00',
    });
    // The admin-only actuals are never sent by a non-admin.
    expect(patchBody).not.toHaveProperty('sell_price_paise');
    expect(patchBody).not.toHaveProperty('freight_paise');
  });

  it('the Edit modal shows the Actual sell/freight inputs ONLY for an admin', async () => {
    function stub(me: () => Response) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          const method = (init?.method ?? 'GET').toUpperCase();
          if (url.endsWith('/me')) return me();
          if (url.match(/\/projects\/11$/)) return json(PROJECT);
          if (url.match(/\/purchase-orders\?/)) return json([]);
          if (url.match(/\/project-products\?/) && method === 'GET') return json([PP_A]);
          if (url.match(/\/products\?/) && method === 'GET') return json([CAT_A, CAT_B]);
          throw new Error(`Unexpected fetch: ${method} ${url}`);
        }),
      );
    }

    // Non-admin (OPERATE, no platform IAM): actual inputs absent.
    stub(meResponseOperate);
    const { unmount } = renderDetail(<ProjectDetail />);
    let alphaRow = (await screen.findByText('Alpha Widget')).closest('tr') as HTMLElement;
    fireEvent.click(within(alphaRow).getByRole('button', { name: /edit alpha widget/i }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText(/actual sell/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/actual freight/i)).not.toBeInTheDocument();
    // The visible fields are still there.
    expect(within(dialog).getByLabelText(/client sell/i)).toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();

    // Admin (platform IAM): actual inputs present.
    stub(meResponseAdmin);
    renderDetail(<ProjectDetail />);
    alphaRow = (await screen.findByText('Alpha Widget')).closest('tr') as HTMLElement;
    fireEvent.click(within(alphaRow).getByRole('button', { name: /edit alpha widget/i }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/actual sell/i)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/actual freight/i)).toBeInTheDocument();
  });
});
