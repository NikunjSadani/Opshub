import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { QuoteSearchPage } from './QuoteSearchPage';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * `GET /me` granting VIEW on the sales_orders module (the quote-search gate).
 * `isAdmin` toggles the IAM platform permission that unlocks the actual-sell + margin
 * columns; a non-admin holds VIEW but not IAM.
 */
function meResponse(isAdmin: boolean): Response {
  return json({
    id: 1,
    email: isAdmin ? 'admin@example.com' : 'viewer@example.com',
    name: isAdmin ? 'Ada Admin' : 'Val Viewer',
    role_id: 1,
    role_name: isAdmin ? 'Administrator' : 'Viewer',
    is_administrator: isAdmin,
    module_levels: { sales_orders: 'VIEW' },
    platform: isAdmin ? ['iam'] : [],
  });
}

function renderWithProviders(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>{node}</ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const CLIENTS = [{ id: '1', name: 'Britannia', code: 'BRI', active: true }];

const QUOTE_ROW = {
  po_line_item_id: '501',
  product_id: '77',
  product_name: 'Cotton T-Shirt',
  brand: 'Comfy',
  model_number: 'CT-100',
  category: 'Apparel',
  uom: 'PCS',
  po_number: 'PO-2026-042',
  client_id: '1',
  client_name: 'Britannia',
  project_code: 'BRI-001',
  po_date: '2026-05-10',
  ordered_qty: '250.000',
  cost_price_paise: 15000,
  client_sell_price_paise: 22000,
  sell_price_paise: 20000,
  margin_pct: 25,
  client_freight_paise: 700,
  freight_paise: 1000,
  packaging_paise: 500,
  handling_paise: 250,
  other_paise: 0,
  tax_rate: '18.00',
};

const TREND = [
  {
    po_date: '2026-01-01',
    po_number: 'PO-2026-001',
    client_name: 'Britannia',
    ordered_qty: '100.000',
    cost_price_paise: 12000,
    client_sell_price_paise: 17000,
    sell_price_paise: 16000,
  },
  {
    po_date: '2026-05-10',
    po_number: 'PO-2026-042',
    client_name: 'Britannia',
    ordered_qty: '250.000',
    cost_price_paise: 15000,
    client_sell_price_paise: 22000,
    sell_price_paise: 20000,
  },
];

/** Stub fetch, recording every requested URL so filter params can be asserted. */
function stubFetch(rowsForSearch: unknown[], isAdmin = true) {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes('/quote-search/trend')) return json(TREND);
      if (url.includes('/quote-search')) return json(rowsForSearch);
      if (url.includes('/projects/clients')) return json(CLIENTS);
      if (url.endsWith('/me')) return meResponse(isAdmin);
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('QuoteSearchPage', () => {
  it('renders priced rows with rupees, dates, and margin (admin)', async () => {
    stubFetch([QUOTE_ROW]); // default: admin (IAM)
    renderWithProviders(<QuoteSearchPage />);

    expect(await screen.findByText('Cotton T-Shirt')).toBeInTheDocument();
    const row = screen.getByText('Cotton T-Shirt').closest('tr') as HTMLElement;
    // Cost, client price, actual sell rendered from integer paise; margin as a percent.
    expect(within(row).getByText('₹150.00')).toBeInTheDocument(); // cost
    expect(within(row).getByText('₹220.00')).toBeInTheDocument(); // client price
    expect(within(row).getByText('₹200.00')).toBeInTheDocument(); // actual sell (admin)
    expect(within(row).getByText('25.0%')).toBeInTheDocument();
    // Date is shown DD/MM/YYYY.
    expect(within(row).getByText('10/05/2026')).toBeInTheDocument();
  });

  it('shows actual-sell + margin + actual-freight columns only for an IAM admin', async () => {
    stubFetch([QUOTE_ROW], true);
    renderWithProviders(<QuoteSearchPage />);
    const row = (await screen.findByText('Cotton T-Shirt')).closest('tr') as HTMLElement;
    // The admin-only columns are present, incl. the actual freight (audit-fixed leak).
    expect(screen.getByText('Actual sell')).toBeInTheDocument();
    expect(screen.getByText('Margin')).toBeInTheDocument();
    expect(screen.getByText('Actual frt')).toBeInTheDocument();
    // Freight column shows the VISIBLE client freight (₹7.00); actual freight (₹10.00) too.
    expect(within(row).getByText('₹7.00')).toBeInTheDocument();
    expect(within(row).getByText('₹10.00')).toBeInTheDocument();
  });

  it('hides the actual sell + margin + actual freight from a non-admin, keeping client figures', async () => {
    stubFetch([QUOTE_ROW], false); // VIEW but NOT IAM
    renderWithProviders(<QuoteSearchPage />);

    expect(await screen.findByText('Cotton T-Shirt')).toBeInTheDocument();
    const row = screen.getByText('Cotton T-Shirt').closest('tr') as HTMLElement;
    // Client price + client freight are still shown.
    expect(within(row).getByText('₹220.00')).toBeInTheDocument();
    expect(within(row).getByText('₹7.00')).toBeInTheDocument(); // client freight visible
    // The actual sell (₹200.00), margin (25.0%) AND actual freight (₹10.00) are gone.
    expect(screen.queryByText('Actual sell')).not.toBeInTheDocument();
    expect(screen.queryByText('Margin')).not.toBeInTheDocument();
    expect(screen.queryByText('Actual frt')).not.toBeInTheDocument();
    expect(within(row).queryByText('₹200.00')).not.toBeInTheDocument();
    expect(within(row).queryByText('25.0%')).not.toBeInTheDocument();
    expect(within(row).queryByText('₹10.00')).not.toBeInTheDocument(); // actual freight masked
  });

  it('drives the budget filter (client price) into budget_*_paise params', async () => {
    const urls = stubFetch([QUOTE_ROW]);
    renderWithProviders(<QuoteSearchPage />);
    await screen.findByText('Cotton T-Shirt');

    fireEvent.change(screen.getByLabelText(/client price min/i), { target: { value: '100' } });

    // 100 rupees -> 10000 paise on the wire.
    await waitFor(
      () =>
        expect(
          urls.some((u) => u.includes('/quote-search') && u.includes('budget_min_paise=10000')),
        ).toBe(true),
      { timeout: 2000 },
    );
  });

  it('drives the q= query param from the keyword box', async () => {
    const urls = stubFetch([QUOTE_ROW]);
    renderWithProviders(<QuoteSearchPage />);
    await screen.findByText('Cotton T-Shirt');

    fireEvent.change(screen.getByLabelText(/keyword/i), { target: { value: 'shirt' } });

    // The debounced filter re-fires the search with the keyword in the query string.
    await waitFor(
      () => expect(urls.some((u) => u.includes('/quote-search') && u.includes('q=shirt'))).toBe(true),
      { timeout: 2000 },
    );
  });

  it('shows an honest empty state when nothing matches', async () => {
    stubFetch([]);
    renderWithProviders(<QuoteSearchPage />);
    expect(await screen.findByText('No matches')).toBeInTheDocument();
  });

  it('opens the price-history trend for a product', async () => {
    stubFetch([QUOTE_ROW]);
    renderWithProviders(<QuoteSearchPage />);
    await screen.findByText('Cotton T-Shirt');

    fireEvent.click(screen.getByRole('button', { name: /trend/i }));

    expect(await screen.findByText('Price history')).toBeInTheDocument();
    // The trend mini-table lists the earlier PO at its historical price.
    expect(await screen.findByText('PO-2026-001')).toBeInTheDocument();
    expect(screen.getByText('₹160.00')).toBeInTheDocument();
  });
});
