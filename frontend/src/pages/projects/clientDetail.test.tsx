import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ClientDetail } from './ClientDetail';
import { ClientsScreen } from './ClientsScreen';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` granting the given level on the PROJECTS module (client.manage = MANAGE). */
function meResponse(level: 'VIEW' | 'OPERATE' | 'MANAGE' = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'Projects Viewer',
    is_administrator: level === 'MANAGE',
    module_levels: { projects: level },
    platform: level === 'MANAGE' ? ['iam', 'settings'] : [],
  });
}

/** A full client profile with one of each child (backend ClientDetailOut). */
const CLIENT_DETAIL = {
  id: 7,
  name: 'Britannia Industries',
  code: 'BRI',
  pan: 'AAACB1234C',
  credit_terms_days: 30,
  active: true,
  access_pin: null,
  gstins: [
    {
      id: 1,
      gstin: '27ABCDE1234F1Z5',
      legal_name: 'Britannia Ltd',
      state_code: '27',
      is_default: true,
      active: true,
    },
  ],
  addresses: [
    {
      id: 2,
      gstin_id: null,
      label: 'Head office',
      line1: 'Plot 5, MIDC',
      line2: null,
      city: 'Mumbai',
      state: 'MH',
      pincode: '400001',
      is_default: true,
      active: true,
    },
  ],
  contacts: [
    {
      id: 3,
      name: 'Asha Rao',
      email: 'asha@bri.example',
      phone: '9800000000',
      designation: 'Buyer',
      is_default: false,
      active: true,
    },
  ],
};

/** Render ClientDetail at /m/projects/clients/7 with the app providers. */
function renderDetail(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/m/projects/clients/7']}>
        <AuthProvider>
          <ToastProvider>
            <Routes>
              <Route path="/m/projects/clients/:id" element={node} />
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

describe('ClientDetail', () => {
  it('renders the client profile with its gstins, addresses and contacts', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);

    // Header + summary grid.
    expect(await screen.findByText('Britannia Industries')).toBeInTheDocument();
    expect(screen.getByText('AAACB1234C')).toBeInTheDocument();
    expect(screen.getByText('30 days')).toBeInTheDocument();
    // One row from each child collection.
    expect(screen.getByText('27ABCDE1234F1Z5')).toBeInTheDocument();
    expect(screen.getByText('Mumbai')).toBeInTheDocument();
    expect(screen.getByText('Asha Rao')).toBeInTheDocument();
    // A default child shows the Default badge.
    expect(screen.getAllByText('Default').length).toBeGreaterThan(0);
  });

  it('adds a GSTIN via the modal and POSTs the entered value (Manage)', async () => {
    let postedBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/clients\/7\/gstins$/) && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json({ id: 9, gstin: '29AAACB1234C1Z8', legal_name: null, state_code: '29', is_default: false, active: true }, 201);
        }
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);
    await screen.findByText('Britannia Industries');

    // The first "Add" button belongs to the GSTINs section.
    fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^GSTIN/i), {
      target: { value: '29AAACB1234C1Z8' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect((postedBody as { gstin: string }).gstin).toBe('29AAACB1234C1Z8');
    // Modal closes on success.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('surfaces a 422 from the backend on a bad GSTIN shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/clients\/7\/gstins$/) && method === 'POST') {
          return json({ detail: 'GSTIN failed checksum validation.' }, 422);
        }
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);
    await screen.findByText('Britannia Industries');

    fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^GSTIN/i), {
      target: { value: '27ABCDE1234F1Z9' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));

    // The FastAPI detail is surfaced (via toast) and the dialog stays open.
    expect(await screen.findByText(/GSTIN failed checksum validation/i)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('soft-deletes a GSTIN through the confirm dialog (Manage)', async () => {
    let deletedUrl: string | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/clients\/gstins\/1$/) && method === 'DELETE') {
          deletedUrl = url;
          return new Response(null, { status: 204 });
        }
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);
    await screen.findByText('Britannia Industries');

    // Deactivate on the GSTIN row → confirm dialog → confirm.
    const gstinRow = screen.getByText('27ABCDE1234F1Z5').closest('tr') as HTMLElement;
    fireEvent.click(within(gstinRow).getByRole('button', { name: 'Deactivate' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }));

    await waitFor(() => expect(deletedUrl).not.toBeNull());
    expect(deletedUrl).toMatch(/\/projects\/clients\/gstins\/1$/);
  });

  it('adds the FIRST GSTIN to a client with an EMPTY collection (E1)', async () => {
    // The empty-collection regression: Section hid its children (incl. the Add modal)
    // in the empty branch, so Add mounted no modal. With the fix the modal renders in
    // both branches.
    const EMPTY_CLIENT = { ...CLIENT_DETAIL, gstins: [], addresses: [], contacts: [] };
    let postedBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/clients\/7\/gstins$/) && method === 'POST') {
          postedBody = JSON.parse(String(init?.body));
          return json(
            { id: 9, gstin: '29AAACB1234C1Z8', legal_name: null, state_code: '29', is_default: false, active: true },
            201,
          );
        }
        if (url.match(/\/projects\/clients\/7$/)) return json(EMPTY_CLIENT);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);
    await screen.findByText('Britannia Industries');
    // The empty state is shown…
    expect(screen.getByText('No GSTINs on file.')).toBeInTheDocument();

    // …yet the first Add (GSTINs) still opens a working modal.
    fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^GSTIN/i), {
      target: { value: '29AAACB1234C1Z8' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect((postedBody as { gstin: string }).gstin).toBe('29AAACB1234C1Z8');
  });

  it('sends access_pin in the client update payload (challan QR PIN)', async () => {
    let patchedBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/clients\/7$/) && method === 'PATCH') {
          patchedBody = JSON.parse(String(init?.body));
          return json({ ...CLIENT_DETAIL, access_pin: 'secret12' });
        }
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);
    await screen.findByText('Britannia Industries');

    // Open the client edit modal.
    fireEvent.click(screen.getByRole('button', { name: /edit client/i }));
    const dialog = await screen.findByRole('dialog');
    // The Access PIN field renders with its hint.
    const pinField = within(dialog).getByLabelText(/Access PIN/i);
    fireEvent.change(pinField, { target: { value: 'secret12' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(patchedBody).not.toBeNull());
    expect((patchedBody as { access_pin: string }).access_pin).toBe('secret12');
  });

  it('is fully read-only for a VIEW-only user (no add / edit / deactivate)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/projects\/clients\/7$/)) return json(CLIENT_DETAIL);
        if (url.endsWith('/me')) return meResponse('VIEW');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDetail(<ClientDetail />);

    // The profile still renders in full…
    expect(await screen.findByText('Britannia Industries')).toBeInTheDocument();
    expect(screen.getByText('27ABCDE1234F1Z5')).toBeInTheDocument();
    expect(screen.getByText('Asha Rao')).toBeInTheDocument();
    // …but no management affordances are present.
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /edit client/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
  });
});

// M3: the Clients registry is readable at VIEW (so client detail — whose only inbound
// link is this list — is reachable), but the New-client affordance is MANAGE-gated.
describe('ClientsScreen (M3 — list readable at VIEW)', () => {
  const CLIENTS_LIST = [
    { id: 7, name: 'Britannia Industries', code: 'BRI', pan: null, credit_terms_days: null, active: true },
  ];

  function renderClients() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AuthProvider>
            <ToastProvider>
              <ClientsScreen />
            </ToastProvider>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('a VIEW user sees the client list + a link to detail, but no New client', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/projects\/clients$/)) return json(CLIENTS_LIST);
        if (url.endsWith('/me')) return meResponse('VIEW');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderClients();

    expect(await screen.findByText('Britannia Industries')).toBeInTheDocument();
    // The name links to the client's detail (reachable at VIEW).
    const link = screen.getByRole('link', { name: 'Britannia Industries' });
    expect(link.getAttribute('href')).toContain('7');
    // …but no create affordance.
    expect(screen.queryByRole('button', { name: /new client/i })).not.toBeInTheDocument();
  });

  it('a MANAGE user gets the New client button', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/projects\/clients$/)) return json(CLIENTS_LIST);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderClients();

    expect(await screen.findByRole('button', { name: /new client/i })).toBeInTheDocument();
  });
});
