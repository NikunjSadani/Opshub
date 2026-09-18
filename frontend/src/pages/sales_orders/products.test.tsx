import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ProductsPage } from './ProductsPage';
import { ProductsUpload } from './ProductsUpload';

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
    gst_rate: '18.00',
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
    gst_rate: null,
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

  it('sends gst_rate (as a string) on create', async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ ...PRODUCTS[0], id: '9', name: 'Taxed Item', code: null }, 201);
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    await screen.findByText('15W LED Bulb');

    fireEvent.click(screen.getByRole('button', { name: /new product/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/name/i), { target: { value: 'Taxed Item' } });
    fireEvent.change(within(dialog).getByLabelText(/gst rate/i), { target: { value: '18.5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /create product/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    // Sent as a STRING (mirrors tax_rate) — no NaN / precision loss.
    expect(postedBody).toMatchObject({ name: 'Taxed Item', gst_rate: '18.5' });
  });

  it('round-trips gst_rate on edit and sends null when cleared', async () => {
    let firstBody: Record<string, unknown> | null = null;
    let secondBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products/1') && method === 'PATCH') {
          const body = JSON.parse(String(init?.body));
          if (firstBody === null) firstBody = body;
          else secondBody = body;
          return json({ ...PRODUCTS[0] }, 200);
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    const row = (await screen.findByText('15W LED Bulb')).closest('tr') as HTMLElement;

    // Edit #1: the current GST value ("18.00") is seeded into the form and sent back verbatim.
    fireEvent.click(within(row).getByRole('button', { name: /^edit$/i }));
    let dialog = await screen.findByRole('dialog', { name: /edit product/i });
    expect(within(dialog).getByLabelText(/gst rate/i)).toHaveValue('18.00');
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }));
    await waitFor(() => expect(firstBody).not.toBeNull());
    expect(firstBody).toMatchObject({ gst_rate: '18.00' });

    // Edit #2: clearing the field sends an explicit null (clears it on the backend).
    fireEvent.click(within(row).getByRole('button', { name: /^edit$/i }));
    dialog = await screen.findByRole('dialog', { name: /edit product/i });
    fireEvent.change(within(dialog).getByLabelText(/gst rate/i), { target: { value: '' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }));
    await waitFor(() => expect(secondBody).not.toBeNull());
    expect(secondBody!.gst_rate).toBeNull();
  });

  it('blocks an out-of-range gst_rate inline', async () => {
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
    await screen.findByText('15W LED Bulb');

    fireEvent.click(screen.getByRole('button', { name: /new product/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/name/i), { target: { value: 'Bad Tax' } });
    fireEvent.change(within(dialog).getByLabelText(/gst rate/i), { target: { value: '150' } });

    expect(within(dialog).getByText(/Enter a GST rate from 0 to 100\./i)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /create product/i })).toBeDisabled();
  });

  it('Sync HSN master POSTs the endpoint and surfaces created/updated counts + conflicts', async () => {
    let syncMethod: string | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products/sync-hsn-master') && method === 'POST') {
          syncMethod = method;
          return json({
            created: ['85399090'],
            updated: [{ hsn: '94054090', old_rate: '12', new_rate: '18' }],
            conflicts: [
              { hsn: '84713090', rates: ['18', '12'], product_ids: [3, 7] },
            ],
          });
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    await screen.findByText('15W LED Bulb');

    fireEvent.click(screen.getByRole('button', { name: /sync hsn master/i }));

    // The results modal shows the created/updated counts AND the conflict (hsn + rates).
    const dialog = await screen.findByRole('dialog', { name: /hsn master sync/i });
    expect(within(dialog).getByText(/HSN 84713090/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/conflicting rates 18, 12/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/85399090/)).toBeInTheDocument();
    expect(within(dialog).getByText(/12% → 18%/)).toBeInTheDocument();
    expect(syncMethod).toBe('POST');
  });

  it('hides the Sync HSN master button for a non-MANAGE user', async () => {
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
    await screen.findByText('15W LED Bulb');

    expect(screen.queryByRole('button', { name: /sync hsn master/i })).not.toBeInTheDocument();
  });

  it('Copy duplicates a product: opens a prefilled create modal with a cleared code, POSTs it', async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products') && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ ...PRODUCTS[0], id: '9', code: null }, 201);
        }
        if (url.includes('/products')) return json(PRODUCTS);
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsPage />);
    const row = (await screen.findByText('15W LED Bulb')).closest('tr') as HTMLElement;

    fireEvent.click(within(row).getByRole('button', { name: /copy/i }));
    const dialog = await screen.findByRole('dialog', { name: /duplicate product/i });
    // Prefilled from the source product, but the unique CODE is cleared for the copy.
    expect(within(dialog).getByLabelText(/name/i)).toHaveValue('15W LED Bulb');
    expect(within(dialog).getByLabelText(/brand/i)).toHaveValue('Philips');
    expect(within(dialog).getByLabelText(/^code/i)).toHaveValue('');

    fireEvent.click(within(dialog).getByRole('button', { name: /create product/i }));
    await waitFor(() => expect(postedBody).not.toBeNull());
    // It POSTs a CREATE carrying the copied fields, and no code (a fresh one is required).
    expect(postedBody).toMatchObject({ name: '15W LED Bulb', brand: 'Philips', uom: 'PCS' });
    // The cleared code is omitted from the create body (a copy needs its own code).
    expect(postedBody).not.toHaveProperty('code');
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
    // …but no create/edit/upload affordances.
    expect(screen.queryByRole('button', { name: /new product/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^edit$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /upload \.xlsx/i })).not.toBeInTheDocument();
  });
});

describe('Products bulk upload', () => {
  it('posts the multipart file and renders created / updated + an error row (never hidden)', async () => {
    let uploadMethod: string | null = null;
    let uploadWasMultipart = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products/upload') && method === 'POST') {
          uploadMethod = method;
          uploadWasMultipart = init?.body instanceof FormData;
          return json(
            {
              created: ['LED-15'],
              updated: ['EXT-BRD'],
              errors: [{ row: 4, message: 'unknown UOM "SPOON"' }],
            },
            201,
          );
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderWithProviders(<ProductsUpload />);

    const fileInput = document.getElementById('products-file') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(['x'], 'products.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });

    const uploadBtn = screen.getByRole('button', { name: /^upload$/i });
    await waitFor(() => expect(uploadBtn).toBeEnabled());
    fireEvent.click(uploadBtn);

    // Created + updated codes AND the row error (with its message) are all surfaced.
    expect(await screen.findByText('LED-15')).toBeInTheDocument();
    expect(screen.getByText('EXT-BRD')).toBeInTheDocument();
    expect(screen.getByText(/unknown UOM "SPOON"/i)).toBeInTheDocument();
    expect(screen.getByText(/Row 4/i)).toBeInTheDocument();

    // The file was sent as a multipart POST.
    expect(uploadMethod).toBe('POST');
    expect(uploadWasMultipart).toBe(true);
  });

  it('"Download template" GETs the auth-gated products template endpoint and saves the .xlsx', async () => {
    // jsdom lacks blob-URL plumbing; stub it so the download helper's save step is inert.
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();

    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        if (url.includes('/products/bulk-template.xlsx')) {
          return new Response(new Blob(['xlsx-bytes']), {
            status: 200,
            headers: {
              'content-type':
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              'content-disposition': 'attachment; filename="products-bulk-template.xlsx"',
            },
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<ProductsUpload />);

    fireEvent.click(await screen.findByRole('button', { name: /download template/i }));

    await waitFor(() =>
      expect(urls.some((u) => u.endsWith('/api/v1/products/bulk-template.xlsx'))).toBe(true),
    );
  });
});
