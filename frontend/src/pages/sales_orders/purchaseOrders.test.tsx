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
  // The `/products` search (ProductOut) carries gst_rate — the picker pre-fills a line's tax.
  { id: '5', code: 'P1', name: 'Widget', brand: 'Acme', model_number: null, uom: 'PCS', gst_rate: '18.00' },
  // A second product at a DIFFERENT rate, to prove swapping a line's product re-derives the
  // auto-filled tax (Widget 18% → Gadget 5%).
  { id: '6', code: 'P2', name: 'Gadget', brand: 'Acme', model_number: null, uom: 'PCS', gst_rate: '5.00' },
];

/** A project's tagged product WITH a pricing template (ProjectProductOut). Money is paise;
 * sell_price_paise/freight_paise are the admin-only actuals. */
const TEMPLATE_PRODUCT = {
  product_id: 5,
  code: 'P1',
  name: 'Widget',
  brand: 'Acme',
  model_number: null,
  category: 'Widgets',
  active: true,
  product_uom: 'PCS',
  description: 'Templated blue widget',
  uom: 'BOX',
  cost_price_paise: 10000, // Our CP ₹100.00
  original_cost_price_paise: 9000,
  client_sell_price_paise: 25000, // Client sell ₹250.00
  vendor_sell_price_paise: 20000,
  sell_price_paise: 24000, // Actual sell ₹240.00 (admin-only)
  client_freight_paise: 1000,
  vendor_freight_paise: 800,
  freight_paise: 900, // Actual freight ₹9.00 (admin-only)
  packaging_paise: 500,
  handling_paise: 0,
  other_paise: 0,
  tax_rate: '18.00',
};
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
  // Admin caller (MANAGE → platform iam) — the ACTUAL sell total is present. A non-admin
  // caller would receive `total_sell_paise: null` (see PO_ROW_MASKED).
  total_sell_paise: 2500000,
  total_client_sell_paise: 2500000,
  total_client_freight_paise: 0,
  total_client_extras_paise: 0,
  agency_fee_type: 'NONE',
  agency_fee_percent: null,
  agency_fee_amount_paise: null,
  agency_fee_computed_paise: 0,
  total_with_agency_paise: 2500000,
  created_at: '2026-05-10T00:00:00Z',
};

// The same row as a NON-admin sees it: the ACTUAL sell total is masked to null; the
// client-facing order value is still present so the register never renders ₹0.00.
const PO_ROW_MASKED = {
  ...PO_ROW,
  total_sell_paise: null,
  total_client_sell_paise: 2400000,
  total_with_agency_paise: 2400000,
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
      original_cost_price_paise: 90000,
      client_sell_price_paise: 125000,
      vendor_sell_price_paise: 110000,
      // Actual sell/freight are admin-only — masked (null) for a non-admin caller.
      sell_price_paise: null,
      client_freight_paise: 0,
      vendor_freight_paise: 0,
      freight_paise: null,
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

/** Pick an option from the searchable product combobox (open → type → click the match). */
async function selectProduct(match: RegExp, type?: string) {
  const combo = screen.getByRole('combobox', { name: /product/i });
  fireEvent.focus(combo);
  if (type) fireEvent.change(combo, { target: { value: type } });
  const opt = await screen.findByRole('option', { name: match });
  fireEvent.mouseDown(opt);
}

/** Pick an option from a SearchableSelect combobox (Client / Project are now searchable):
 * focus to open the listbox, then mousedown the matching option (commits on mousedown). The
 * picker is disabled until its options query resolves — wait, else focus is a no-op. */
async function pickCombo(labelRe: RegExp, optionRe: RegExp) {
  const combo = screen.getByRole('combobox', { name: labelRe });
  await waitFor(() => expect(combo).toBeEnabled());
  fireEvent.focus(combo);
  const opt = await screen.findByRole('option', { name: optionRe });
  fireEvent.mouseDown(opt);
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
    // The register "Order value" column renders total_with_agency_paise (2500000) in rupees.
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

  it('a NON-admin sees the client-facing order value, never ₹0.00 for the masked actual', async () => {
    // OPERATE → no platform perms → non-admin: the API masks total_sell_paise to null.
    // Regression guard for the HIGH the E2E audit caught — the register must render the
    // client-facing order value (total_with_agency_paise), not a ₹0.00 from the null actual.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/purchase-orders')) return json([PO_ROW_MASKED]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<PurchaseOrdersPage />);

    expect(await screen.findByText('PO-2026-001')).toBeInTheDocument();
    const row = screen.getByText('PO-2026-001').closest('tr') as HTMLElement;
    // The order-value cell shows ₹24,000.00 (total_with_agency_paise), NOT ₹0.00.
    expect(within(row).getByText('₹24,000.00')).toBeInTheDocument();
    expect(within(row).queryByText('₹0.00')).not.toBeInTheDocument();
  });
});

/** Stub the create-form's supporting GETs for the given /me level; returns captured state. */
function stubCreateForm(level: Level) {
  const state: { postedBody: Record<string, unknown> | null; urls: string[] } = {
    postedBody: null,
    urls: [],
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      state.urls.push(url);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.endsWith('/me')) return meResponse(level);
      // No pricing template by default — the "Load this project's products" button hides.
      if (url.includes('/project-products')) return json([]);
      if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
      if (url.includes('/projects/clients')) return json(CLIENTS);
      if (url.includes('/projects')) return json(PROJECTS);
      if (url.includes('/products')) return json(PRODUCTS);
      if (url.includes('/purchase-orders') && method === 'POST') {
        state.postedBody = JSON.parse(String(init?.body));
        return json({ ...PO_DETAIL }, 201);
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    }),
  );
  return state;
}

/** Fill the header (client + project + date) plus one valid line's required money. */
async function fillMinimalValidForm() {
  await pickCombo(/^client ?\*/i, /BRI — Britannia/);
  await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);
  fireEvent.change(screen.getByLabelText(/^po date/i), { target: { value: '2026-05-10' } });
  await selectProduct(/Widget/);
  fireEvent.change(screen.getByLabelText(/ordered qty/i), { target: { value: '2' } });
  fireEvent.change(screen.getByLabelText(/our cp/i), { target: { value: '100.50' } });
  fireEvent.change(screen.getByLabelText(/client sell/i), { target: { value: '250' } });
}

describe('PO create form', () => {
  it('submits the full pricing set (original CP, client/vendor/actual sell, freight, agency fee) as paise', async () => {
    // MANAGE → platform ['iam'] → admin, so the Actual sell/freight inputs render.
    const state = stubCreateForm('MANAGE');
    renderWithProviders(<POForm />);

    fireEvent.change(await screen.findByLabelText(/po number/i), { target: { value: 'PO-NEW-1' } });
    await fillMinimalValidForm();

    fireEvent.change(screen.getByLabelText(/original cp/i), { target: { value: '90' } });
    fireEvent.change(screen.getByLabelText(/vendor sell/i), { target: { value: '200' } });
    fireEvent.change(screen.getByLabelText(/actual sell/i), { target: { value: '240' } });
    fireEvent.change(screen.getByLabelText(/client freight/i), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText(/vendor freight/i), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText(/actual freight/i), { target: { value: '9' } });

    // Agency fee: fixed ₹5000.
    fireEvent.change(screen.getByLabelText(/^agency fee$/i), { target: { value: 'FIXED' } });
    fireEvent.change(screen.getByLabelText(/agency fee ₹/i), { target: { value: '5000' } });

    const submit = screen.getByRole('button', { name: /create purchase order/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(state.postedBody).not.toBeNull());
    const body = state.postedBody as unknown as {
      po_number: string;
      agency_fee_type: string;
      agency_fee_amount_paise: number;
      lines: Array<Record<string, number | string>>;
    };
    expect(body.po_number).toBe('PO-NEW-1');
    const line = body.lines[0];
    expect(line.product_id).toBe('5');
    expect(line.cost_price_paise).toBe(10050); // Our CP ₹100.50
    expect(line.original_cost_price_paise).toBe(9000); // ₹90
    expect(line.client_sell_price_paise).toBe(25000); // ₹250
    expect(line.vendor_sell_price_paise).toBe(20000); // ₹200
    expect(line.sell_price_paise).toBe(24000); // Actual sell ₹240 (admin)
    expect(line.client_freight_paise).toBe(1000); // ₹10
    expect(line.vendor_freight_paise).toBe(800); // ₹8
    expect(line.freight_paise).toBe(900); // Actual freight ₹9 (admin)
    expect(body.agency_fee_type).toBe('FIXED');
    expect(body.agency_fee_amount_paise).toBe(500000); // ₹5000
  });

  it('an ADMIN sees the Actual sell/freight inputs; a non-admin does NOT', async () => {
    // Admin (MANAGE → platform iam).
    stubCreateForm('MANAGE');
    const { unmount } = renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    expect(screen.getByLabelText(/actual sell/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/actual freight/i)).toBeInTheDocument();
    unmount();

    // Non-admin (OPERATE → no platform perms).
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    expect(screen.queryByLabelText(/actual sell/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/actual freight/i)).not.toBeInTheDocument();
    // The visible sell/freight fields are still there.
    expect(screen.getByLabelText(/client sell/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/vendor freight/i)).toBeInTheDocument();
  });

  it('"Compute from 50%" sets Our CP = Original CP + (Vendor sell − Original CP) / 2', async () => {
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Helper is disabled until both Original CP and Vendor sell are present.
    const computeBtn = screen.getByRole('button', { name: /compute from 50%/i });
    expect(computeBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/original cp/i), { target: { value: '100' } });
    fireEvent.change(screen.getByLabelText(/vendor sell/i), { target: { value: '200' } });
    expect(computeBtn).toBeEnabled();
    fireEvent.click(computeBtn);

    // 100 + (200 − 100)/2 = 150.
    expect((screen.getByLabelText(/our cp/i) as HTMLInputElement).value).toBe('150.00');
  });

  it('creates a PO WITHOUT a po_number (optional now)', async () => {
    const state = stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Leave PO number blank; fill everything else required.
    await fillMinimalValidForm();

    const submit = screen.getByRole('button', { name: /create purchase order/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(state.postedBody).not.toBeNull());
    const body = state.postedBody as unknown as { po_number?: string | null };
    // Blank number is omitted (undefined) — not sent as an empty string.
    expect(body.po_number).toBeUndefined();
  });

  it('the product picker issues a server-side search query on typing', async () => {
    const state = stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    const combo = screen.getByRole('combobox', { name: /product/i });
    fireEvent.focus(combo);
    fireEvent.change(combo, { target: { value: 'wid' } });

    // Debounced fetch hits /products with the typed query.
    await waitFor(() =>
      expect(state.urls.some((u) => u.includes('/products') && u.includes('q=wid'))).toBe(true),
    );
  });
});

describe('PO create form — tax pre-fill from the product GST rate', () => {
  it("picking a product for a blank line pre-fills the line's tax rate from its gst_rate", async () => {
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Blank line: pick Widget (gst_rate "18.00") → the tax input pre-fills to 18.00.
    await selectProduct(/Widget/);
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('18.00');
  });

  it('picking a product does NOT overwrite a tax rate the operator already typed', async () => {
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Operator types a rate FIRST; picking a product must leave it untouched.
    fireEvent.change(screen.getByLabelText(/tax rate/i), { target: { value: '9' } });
    await selectProduct(/Widget/);
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('9');
  });

  it('SWAPPING a line to a different product re-derives an auto-filled tax rate', async () => {
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Pick Widget → auto-fills 18.00…
    await selectProduct(/Widget/);
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('18.00');
    // …then swap the SAME line to Gadget (5%): the auto-filled rate updates to 5.00, not 18.
    await selectProduct(/Gadget/);
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('5.00');
  });

  it('SWAPPING a product does NOT re-derive a rate the operator typed', async () => {
    stubCreateForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);

    // Pick Widget (auto 18) → operator OVERRIDES to 12 → swap to Gadget: their 12 survives.
    await selectProduct(/Widget/);
    fireEvent.change(screen.getByLabelText(/tax rate/i), { target: { value: '12' } });
    await selectProduct(/Gadget/);
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('12');
  });
});

describe('PO create form — load this project\'s products (template pre-fill)', () => {
  /** Stub the create form's GETs, serving this project's tagged product WITH a template. */
  function stubLoadForm(level: Level) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse(level);
        if (url.includes('/project-products')) return json([TEMPLATE_PRODUCT]);
        if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );
  }

  it('appends a pre-filled line per tagged product with qty LEFT BLANK (non-admin)', async () => {
    stubLoadForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);

    // The button appears once the project's template loads; clicking it fills a line.
    const loadBtn = await screen.findByRole('button', { name: /load this project's products/i });
    fireEvent.click(loadBtn);

    // Template money (paise) → the line's rupee inputs; description from the template.
    expect((screen.getByLabelText(/our cp/i) as HTMLInputElement).value).toBe('100.00');
    expect((screen.getByLabelText(/client sell/i) as HTMLInputElement).value).toBe('250.00');
    expect((screen.getByLabelText(/vendor sell/i) as HTMLInputElement).value).toBe('200.00');
    expect((screen.getByLabelText(/client freight/i) as HTMLInputElement).value).toBe('10.00');
    expect((screen.getByLabelText(/tax rate/i) as HTMLInputElement).value).toBe('18.00');
    expect((screen.getByLabelText(/description/i) as HTMLInputElement).value).toBe(
      'Templated blue widget',
    );
    // ordered_qty is LEFT BLANK for the operator to enter.
    expect((screen.getByLabelText(/ordered qty/i) as HTMLInputElement).value).toBe('');
    // A non-admin never sees (or pre-fills) the admin-only actuals.
    expect(screen.queryByLabelText(/actual sell/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/actual freight/i)).not.toBeInTheDocument();
  });

  it('clicking "Load this project\'s products" twice does not duplicate the line', async () => {
    stubLoadForm('OPERATE');
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);
    const loadBtn = await screen.findByRole('button', { name: /load this project's products/i });
    fireEvent.click(loadBtn);
    fireEvent.click(loadBtn);
    // Deduped by product_id → still exactly one line for the single template product.
    expect(screen.getAllByLabelText(/our cp/i)).toHaveLength(1);
  });

  it("pre-fills the line UOM from the product master when the template's uom is null", async () => {
    // Template with NO uom override but the Product master carries "CTN" (product_uom).
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/project-products'))
          return json([{ ...TEMPLATE_PRODUCT, uom: null, product_uom: 'CTN' }]);
        if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);
    fireEvent.click(await screen.findByRole('button', { name: /load this project's products/i }));

    // Template uom was null → falls back to the product master's UOM.
    expect((screen.getByLabelText(/uom/i) as HTMLInputElement).value).toBe('CTN');
  });

  it('an ADMIN also gets the actual sell/freight pre-filled from the template', async () => {
    stubLoadForm('MANAGE'); // MANAGE → platform iam → admin
    renderWithProviders(<POForm />);
    await screen.findByLabelText(/po number/i);
    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);
    fireEvent.click(await screen.findByRole('button', { name: /load this project's products/i }));

    expect((screen.getByLabelText(/actual sell/i) as HTMLInputElement).value).toBe('240.00');
    expect((screen.getByLabelText(/actual freight/i) as HTMLInputElement).value).toBe('9.00');
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

describe('PO confirm flow', () => {
  it('a DRAFT PO shows Confirm and clicking it POSTs to /confirm; the badge flips to Confirmed', async () => {
    let confirmUrl: string | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (/\/purchase-orders\/\d+\/confirm$/.test(url) && method === 'POST') {
          confirmUrl = url;
          return json({ ...PO_DETAIL, id: 1, status: 'CONFIRMED' });
        }
        if (/\/purchase-orders\/\d+$/.test(url)) return json({ ...PO_DETAIL, id: 1 });
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    // Opens as Draft with the Confirm button available to an OPERATE user.
    expect(await screen.findByText('Draft')).toBeInTheDocument();
    const confirmBtn = await screen.findByRole('button', { name: /confirm po/i });
    fireEvent.click(confirmBtn);

    // POSTs to the /confirm endpoint and the badge live-updates to Confirmed.
    await waitFor(() => expect(confirmUrl).not.toBeNull());
    expect(confirmUrl).toMatch(/\/purchase-orders\/1\/confirm$/);
    expect(await screen.findByText('Confirmed')).toBeInTheDocument();
    // Focus moves to the status region (the Confirm button just unmounted) — not dropped to body.
    await waitFor(() =>
      expect(screen.getByText('Confirmed').closest('span[tabindex="-1"]')).toBe(
        document.activeElement,
      ),
    );
  });

  it('a CONFIRMED PO does NOT show the Confirm button', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (/\/purchase-orders\/\d+$/.test(url))
          return json({ ...PO_DETAIL, id: 1, status: 'CONFIRMED' });
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    expect(await screen.findByText('Confirmed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm po/i })).not.toBeInTheDocument();
  });

  it('a VIEW-only user does not see Confirm on a DRAFT PO', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('VIEW');
        if (/\/purchase-orders\/\d+$/.test(url)) return json({ ...PO_DETAIL, id: 1 });
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    expect(await screen.findByText('Draft')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm po/i })).not.toBeInTheDocument();
  });

  it('a failed confirm (already-confirmed elsewhere) reconciles: badge flips, button clears', async () => {
    // A concurrent operator already confirmed it; our cache is stale DRAFT. The POST 422s,
    // and the onError refetch must pull the true CONFIRMED state (not leave a dead button).
    let confirmAttempted = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (/\/purchase-orders\/\d+\/confirm$/.test(url) && method === 'POST') {
          confirmAttempted = true;
          return json({ detail: 'only a DRAFT purchase order can be confirmed' }, 422);
        }
        if (/\/purchase-orders\/\d+$/.test(url))
          return json({ ...PO_DETAIL, id: 1, status: confirmAttempted ? 'CONFIRMED' : 'DRAFT' });
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    const confirmBtn = await screen.findByRole('button', { name: /confirm po/i });
    fireEvent.click(confirmBtn);

    // The onError invalidation refetches -> badge reconciles to Confirmed, stale button clears.
    expect(await screen.findByText('Confirmed')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /confirm po/i })).not.toBeInTheDocument(),
    );
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

    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);

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

  it('"Download template" GETs the auth-gated template endpoint and saves the .xlsx', async () => {
    // jsdom lacks blob-URL plumbing; stub it so the download helper's save step is inert.
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();

    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/purchase-orders/bulk-template.xlsx')) {
          return new Response(new Blob(['xlsx-bytes']), {
            status: 200,
            headers: {
              'content-type':
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              'content-disposition': 'attachment; filename="po-bulk-template.xlsx"',
            },
          });
        }
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/projects')) return json(PROJECTS);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<POUpload />);

    const btn = await screen.findByRole('button', { name: /download template/i });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(urls.some((u) => u.endsWith('/api/v1/purchase-orders/bulk-template.xlsx'))).toBe(
        true,
      ),
    );
  });
});

describe('PO detail live-updates after a mutation (E2)', () => {
  it('reflects a void immediately — setQueryData lands on the SAME cache key', async () => {
    // The backend returns `id` as a runtime NUMBER while the detail query is keyed on
    // the URL param STRING. Before the fix the mutation wrote to a different (number)
    // cache key, so the open detail never updated. It is normalised with String() now.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (/\/purchase-orders\/\d+\/void$/.test(url) && method === 'POST') {
          return json({ ...PO_DETAIL, id: 1, status: 'CANCELLED' });
        }
        if (/\/purchase-orders\/\d+$/.test(url)) return json({ ...PO_DETAIL, id: 1 });
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    // Opens as Draft.
    expect(await screen.findByText('Draft')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^void$/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/reason/i), {
      target: { value: 'Cancelled by client' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: /void purchase order/i }));

    // The open detail flips to Cancelled with NO manual refetch (the detail query is
    // never invalidated — only the mutation's setQueryData drives this).
    expect(await screen.findByText('Cancelled')).toBeInTheDocument();
  });
});

describe('PO detail — add PO number later', () => {
  it('a PO created without a number shows "Add PO number" and PATCHes the amend endpoint', async () => {
    const PO_NO_NUMBER = { ...PO_DETAIL, id: 1, po_number: null };
    let patchUrl: string | null = null;
    let patchBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (/\/purchase-orders\/\d+$/.test(url) && method === 'PATCH') {
          patchUrl = url;
          patchBody = JSON.parse(String(init?.body));
          return json({ ...PO_NO_NUMBER, po_number: 'PO-LATER-9' });
        }
        if (/\/purchase-orders\/\d+$/.test(url)) return json(PO_NO_NUMBER);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/m/sales_orders/:id" element={<PODetail />} />
      </Routes>,
      ['/m/sales_orders/1'],
    );

    // The title shows the no-number placeholder and the add affordance appears.
    expect(await screen.findByText(/PO \(no number\)/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /add po number/i }));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/po number/i), {
      target: { value: 'PO-LATER-9' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: /save amendment/i }));

    await waitFor(() => expect(patchUrl).not.toBeNull());
    expect(patchUrl).toMatch(/\/purchase-orders\/1$/);
    expect((patchBody as unknown as { po_number: string }).po_number).toBe('PO-LATER-9');
    // The refreshed detail reflects the newly-added number.
    expect(await screen.findByText(/PO PO-LATER-9/)).toBeInTheDocument();
  });
});

describe('PO create form — projects fetch error (M2)', () => {
  it('surfaces a load error (not a false "No active projects") when projects fail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        // Client-detail (for the GSTIN picker) still succeeds — only the project list fails.
        if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/products')) return json(PRODUCTS);
        if (url.includes('/projects')) return json({ detail: 'boom' }, 500);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<POForm />);

    await pickCombo(/^client ?\*/i, /BRI — Britannia/);

    // A distinct load-failure message + retry — NOT the genuinely-empty copy.
    expect(await screen.findByText(/Couldn't load projects/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText(/No active projects for this client/i)).not.toBeInTheDocument();
  });
});

describe('PO create form — soft copy attachment (M4)', () => {
  it('uploads a chosen file and sends its id as soft_copy_file_id on create', async () => {
    let postedBody: Record<string, unknown> | null = null;
    let uploadCalled = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('OPERATE');
        if (url.includes('/project-products')) return json([]);
        if (/\/projects\/clients\/\d+/.test(url)) return json(CLIENT_DETAIL);
        if (url.includes('/projects/clients')) return json(CLIENTS);
        if (url.includes('/products')) return json(PRODUCTS);
        if (url.includes('/projects')) return json(PROJECTS);
        if (url.includes('/files/upload') && method === 'POST') {
          uploadCalled = true;
          return json({ id: 42, filename: 'signed-po.pdf', size: 123 }, 201);
        }
        if (url.includes('/purchase-orders') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ ...PO_DETAIL }, 201);
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<POForm />);

    fireEvent.change(await screen.findByLabelText(/po number/i), { target: { value: 'PO-NEW-2' } });
    await pickCombo(/^client ?\*/i, /BRI — Britannia/);
    await pickCombo(/^project ?\*/i, /BRI-001 — Q3 Trade Rewards/);
    fireEvent.change(screen.getByLabelText(/^po date/i), { target: { value: '2026-05-10' } });
    await selectProduct(/Widget/);
    fireEvent.change(screen.getByLabelText(/ordered qty/i), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/our cp/i), { target: { value: '100' } });
    fireEvent.change(screen.getByLabelText(/client sell/i), { target: { value: '250' } });

    // Attach the soft copy → it uploads, then the filename is shown.
    const fileInput = document.getElementById('po-soft-copy') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['x'], 'signed-po.pdf', { type: 'application/pdf' })] },
    });
    await waitFor(() => expect(uploadCalled).toBe(true));
    expect(await screen.findByText('signed-po.pdf')).toBeInTheDocument();

    const submit = screen.getByRole('button', { name: /create purchase order/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect((postedBody as unknown as { soft_copy_file_id: string }).soft_copy_file_id).toBe('42');
  });
});
