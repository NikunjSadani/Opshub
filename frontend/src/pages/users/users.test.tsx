import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { MockAuthProvider, type Role } from '../../auth/AuthProvider';
import { RequireRole } from '../../auth/RequireRole';
import { ToastProvider } from '../../ui';
import { Sidebar } from '../../components/Sidebar';
import { UsersModule } from './UsersModule';

const USERS = [
  {
    id: 1,
    email: 'ops.admin@gifsy.in',
    name: 'Ops Admin',
    role: 'ADMIN',
    active: true,
    module_keys: ['document_automation'],
    is_provisioned: true,
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2,
    email: 'jane@gifsy.in',
    name: 'Jane Doe',
    role: 'OPERATIONS',
    active: false,
    module_keys: [],
    is_provisioned: false,
    created_at: '2026-02-01T00:00:00Z',
  },
];

const MODULES = [
  { key: 'document_automation', title: 'Document Automation', nav_group: 'Modules', coming_soon: false },
  { key: 'projects', title: 'Projects', nav_group: 'Modules', coming_soon: false },
  // Filtered out by useAssignableModules (system + health).
  { key: 'health', title: 'Health', nav_group: '_system', coming_soon: false },
];

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Wrap a subtree with the app's providers, pinned to a role via the mock. */
function renderWithProviders(node: ReactNode, role: Role = 'ADMIN') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MockAuthProvider initialRole={role}>
          <ToastProvider>{node}</ToastProvider>
        </MockAuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('UsersModule', () => {
  it('renders the user list from GET /users', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/modules')) return json(MODULES);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);

    expect(await screen.findByText('ops.admin@gifsy.in')).toBeInTheDocument();
    expect(screen.getByText('jane@gifsy.in')).toBeInTheDocument();
    // Granted-module chip resolves to the human title.
    expect(await screen.findByText('Document Automation')).toBeInTheDocument();
    // The disabled + unprovisioned user is flagged.
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText(/setup pending/i)).toBeInTheDocument();
  });

  it('posts the right body on invite and shows the one-time setup link', async () => {
    let postBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/users') && method === 'POST') {
          postBody = JSON.parse(String(init?.body));
          return json(
            {
              user: {
                id: 3,
                email: 'new@gifsy.in',
                name: 'New User',
                role: 'OPERATIONS',
                active: true,
                module_keys: ['document_automation'],
                is_provisioned: false,
                created_at: '2026-03-01T00:00:00Z',
              },
              setup_link: 'https://opshub.example/setup?token=abc123',
            },
            201,
          );
        }
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/modules')) return json(MODULES);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);

    fireEvent.click(await screen.findByRole('button', { name: /invite user/i }));

    fireEvent.change(await screen.findByLabelText(/email/i), {
      target: { value: 'new@gifsy.in' },
    });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'New User' } });
    // Grant a module.
    fireEvent.click(await screen.findByLabelText('Document Automation'));

    fireEvent.click(screen.getByRole('button', { name: /send invite/i }));

    // Success view shows the copyable setup link with honest copy.
    expect(
      await screen.findByText(/send this password-setup link to the user/i),
    ).toBeInTheDocument();
    const linkField = screen.getByLabelText(/password setup link/i) as HTMLInputElement;
    expect(linkField.value).toBe('https://opshub.example/setup?token=abc123');

    await waitFor(() => expect(postBody).not.toBeNull());
    expect(postBody).toEqual({
      email: 'new@gifsy.in',
      name: 'New User',
      role: 'OPERATIONS',
      module_keys: ['document_automation'],
    });
  });

  it('shows the honest "no link" note when setup_link is null', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/users') && method === 'POST') {
          return json(
            {
              user: {
                id: 4,
                email: 'z@gifsy.in',
                name: 'Zoe',
                role: 'FINANCE',
                active: true,
                module_keys: [],
                is_provisioned: false,
                created_at: '2026-03-02T00:00:00Z',
              },
              setup_link: null,
            },
            201,
          );
        }
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/modules')) return json(MODULES);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);
    fireEvent.click(await screen.findByRole('button', { name: /invite user/i }));
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: 'z@gifsy.in' } });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'Zoe' } });
    fireEvent.click(screen.getByRole('button', { name: /send invite/i }));

    expect(
      await screen.findByText(/no setup link yet/i),
    ).toBeInTheDocument();
    // Must NOT imply an email was already sent, or that one will be.
    expect(screen.queryByText(/email sent/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/receive a set-password email/i)).not.toBeInTheDocument();
  });

  it('surfaces a 409 guard error (self-lockout) as a readable message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/users/1') && method === 'PATCH') {
          return json({ detail: 'You cannot disable your own account.' }, 409);
        }
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/modules')) return json(MODULES);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);

    // Open the edit modal for the admin (row 1), disable, then save.
    const editButtons = await screen.findAllByRole('button', { name: /^edit$/i });
    fireEvent.click(editButtons[0]);
    fireEvent.click(await screen.findByRole('button', { name: /^disable$/i }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    // The guard message is shown to the user (not raw JSON).
    expect(
      await screen.findByText(/you cannot disable your own account/i),
    ).toBeInTheDocument();
  });

  it('blocks a non-admin at the route guard', async () => {
    // No /users fetch should be needed — the guard renders first.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        throw new Error(`Unexpected fetch: ${String(input)}`);
      }),
    );

    renderWithProviders(
      <RequireRole allow={['ADMIN']}>
        <UsersModule />
      </RequireRole>,
      'OPERATIONS',
    );

    expect(screen.getByText(/not authorized/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /invite user/i })).not.toBeInTheDocument();
  });

  it('hides the Users nav tile from a non-admin and shows it to an admin', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/modules')) return json(MODULES);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    const { unmount } = renderWithProviders(<Sidebar />, 'OPERATIONS');
    await waitFor(() =>
      expect(screen.queryByText('Administration')).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole('link', { name: /^users$/i })).not.toBeInTheDocument();
    unmount();

    renderWithProviders(<Sidebar />, 'ADMIN');
    expect(await screen.findByText('Administration')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^users$/i })).toBeInTheDocument();
  });
});
