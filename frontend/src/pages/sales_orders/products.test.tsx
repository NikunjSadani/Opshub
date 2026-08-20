import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ProductsPage } from './ProductsPage';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * `GET /me` payload the mock provider fetches on sign-in. `sales_orders` at the
 * given level drives the create/edit gate (MANAGE) vs read-only (VIEW).
 */
function meResponse(level: 'VIEW' | 'OPERATE' | 'MANAGE' = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'Sales Viewer',
    is_administrator: level === 'MANAGE',
    module_levels: { sales_orders: level },
    platform: level === 'MANAGE' ? ['iam', 'settings'] : [],
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

const PRODUCTS = [
  {
    id: '1',
    code: 'LED-15',
    name: '15W LED Bulb',
    brand: 'Philips',
    model_number: 'P15',
    category: 'Lighting',
    uom: 'PCS',
    hsn: '8539',
    active: true,
    created_at: '2026-08-01T00:00:00Z',
  },
  {
    id: '2',
    code: null,
    name: 'Extension Board',
    brand: null,
    model_number: null,
    category: 'Accessories',
    uom: 'PCS',
    hsn: null,
    active: false,
    created_at: '2026-08-02T00:00:00Z',
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ProductsPage', () => {
  it('renders the product list from GET /products (MANAGE shows New product + Edit)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);

    expect(await screen.findByText('15W LED Bulb')).toBeInTheDocument();
    expect(screen.getByText('Extension Board')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new product/i })).toBeInTheDocument();
    // Each row has an Edit action for a MANAGE user.
    expect(screen.getAllByRole('button', { name: /^edit$/i })).toHaveLength(2);
  });

  it('passes the category filter to the products request', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products')) {
          urls.push(url);
          return json(PRODUCTS);
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    await screen.findByText('15W LED Bulb');

    fireEvent.change(screen.getByLabelText(/category/i), { target: { value: 'Lighting' } });

    await waitFor(() => expect(urls.some((u) => u.includes('category=Lighting'))).toBe(true));
  });

  it('creates a product: opens the modal, submits, and POSTs the body', async () => {
    let postedBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ ...PRODUCTS[0], id: '9', name: 'Ceiling Fan', code: null }, 201);
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    await screen.findByText('15W LED Bulb');

    fireEvent.click(screen.getByRole('button', { name: /new product/i }));
    const dialog = await screen.findByRole('dialog');
    // UOM defaults to PCS; only the name is needed to enable Create.
    fireEvent.change(within(dialog).getByLabelText(/name/i), { target: { value: 'Ceiling Fan' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /create product/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({ name: 'Ceiling Fan', uom: 'PCS' });
    // The modal closes on success.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('surfaces the server 409 message on a duplicate', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products') && method === 'POST') {
          return json({ detail: 'A product with this code already exists.' }, 409);
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    await screen.findByText('15W LED Bulb');

    fireEvent.click(screen.getByRole('button', { name: /new product/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/name/i), { target: { value: 'Dup Item' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /create product/i }));

    expect(
      await screen.findByText('A product with this code already exists.'),
    ).toBeInTheDocument();
  });

  it('hides the New product button + Edit actions for a VIEW-only user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/me')) return meResponse('VIEW');
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);

    // The list still renders for a viewer…
    expect(await screen.findByText('15W LED Bulb')).toBeInTheDocument();
    // …but no create/edit affordances.
    expect(screen.queryByRole('button', { name: /new product/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^edit$/i })).not.toBeInTheDocument();
  });
});
