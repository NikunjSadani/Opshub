import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { MockAuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { UsersModule } from './UsersModule';

/**
 * User Management tests (RBAC v2). A user holds ONE named role (role_id/role_name);
 * the invite/edit forms pick from the roles list served by GET /roles. There are
 * no more per-user module grants.
 */

// Two users: an admin with a role, and an unprovisioned/disabled user with none.
const USERS = [
  {
    id: 1,
    email: 'ops.admin@gifsy.in',
    name: 'Ops Admin',
    role_id: 10,
    role_name: 'Administrator',
    active: true,
    is_provisioned: true,
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2,
    email: 'jane@gifsy.in',
    name: 'Jane Doe',
    role_id: null,
    role_name: null,
    active: false,
    is_provisioned: false,
    created_at: '2026-02-01T00:00:00Z',
  },
];

// The assignable roles served to the picker by GET /roles.
const ROLES = [
  { id: 10, name: 'Administrator', description: 'Full access', is_system: true },
  { id: 20, name: 'Operations', description: 'Ops desk', is_system: false },
];

// GET /me for the signed-in (mock) admin, so the AuthProvider can resolve.
const ME = {
  id: 1,
  email: 'ops.admin@gifsy.in',
  name: 'Ops Admin',
  role_id: 10,
  role_name: 'Administrator',
  is_administrator: true,
  module_levels: {},
  platform: ['iam'],
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Wrap a subtree with the app's providers (mock auth signs in as the seeded admin). */
function renderWithProviders(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MockAuthProvider>
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
  it('renders the user list with the assigned role from GET /users', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/roles')) return json(ROLES);
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/me')) return json(ME);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);

    expect(await screen.findByText('ops.admin@gifsy.in')).toBeInTheDocument();
    expect(screen.getByText('jane@gifsy.in')).toBeInTheDocument();
    // The assigned role shows as a badge.
    expect(screen.getByText('Administrator')).toBeInTheDocument();
    // The role-less user renders an em dash, not "null".
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.queryByText(/null/i)).not.toBeInTheDocument();
    // The disabled + unprovisioned user is flagged.
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText(/setup pending/i)).toBeInTheDocument();
  });

  it('posts { email, name, role_id } on invite and shows the one-time setup link', async () => {
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
                role_id: 20,
                role_name: 'Operations',
                active: true,
                is_provisioned: false,
                created_at: '2026-03-01T00:00:00Z',
              },
              setup_link: 'https://opshub.example/setup?token=abc123',
            },
            201,
          );
        }
        if (url.includes('/roles')) return json(ROLES);
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/me')) return json(ME);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);

    fireEvent.click(await screen.findByRole('button', { name: /invite user/i }));

    fireEvent.change(await screen.findByLabelText(/email/i), {
      target: { value: 'new@gifsy.in' },
    });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'New User' } });

    // The role picker lists the roles from GET /roles; choose one by id.
    const roleSelect = (await screen.findByLabelText(/role/i)) as HTMLSelectElement;
    expect(screen.getByRole('option', { name: 'Administrator' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Operations' })).toBeInTheDocument();
    fireEvent.change(roleSelect, { target: { value: '20' } });

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
      role_id: 20,
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
                role_id: 20,
                role_name: 'Operations',
                active: true,
                is_provisioned: false,
                created_at: '2026-03-02T00:00:00Z',
              },
              setup_link: null,
            },
            201,
          );
        }
        if (url.includes('/roles')) return json(ROLES);
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/me')) return json(ME);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderWithProviders(<UsersModule />);
    fireEvent.click(await screen.findByRole('button', { name: /invite user/i }));
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: 'z@gifsy.in' } });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'Zoe' } });
    // Role auto-defaults to the first available role; just submit.
    fireEvent.click(screen.getByRole('button', { name: /send invite/i }));

    expect(await screen.findByText(/no setup link yet/i)).toBeInTheDocument();
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
        if (url.includes('/roles')) return json(ROLES);
        if (url.includes('/users')) return json(USERS);
        if (url.includes('/me')) return json(ME);
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
});
