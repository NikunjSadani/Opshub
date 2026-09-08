import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ProjectsList } from './ProjectsList';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` granting the given level on the PROJECTS module. */
function meResponse(level: 'VIEW' | 'OPERATE' | 'MANAGE' = 'MANAGE'): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: level === 'MANAGE' ? 'Administrator' : 'Projects User',
    is_administrator: level === 'MANAGE',
    module_levels: { projects: level },
    platform: level === 'MANAGE' ? ['iam', 'settings'] : [],
  });
}

const CLIENTS = [
  { id: 7, name: 'Britannia Industries', code: 'BRI', pan: null, credit_terms_days: null, active: true, access_pin: null },
];

const PROJECT = {
  id: 11,
  code: 'BRI-001',
  client_id: 7,
  client_code: 'BRI',
  client_name: 'Britannia Industries',
  name: 'Q3 Trate Scheme',
  start_date: '2026-07-01',
  status: 'ACTIVE' as const,
  description: 'first draft',
  created_at: '2026-07-01T00:00:00Z',
};

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <ProjectsList />
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

describe('ProjectsList — edit project (MANAGE)', () => {
  it('edits a project name via the modal and PATCHes only editable fields', async () => {
    let patchedBody: unknown = null;
    let patchedUrl: string | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/11$/) && method === 'PATCH') {
          patchedUrl = url;
          patchedBody = JSON.parse(String(init?.body));
          return json({ ...PROJECT, name: 'Q3 Trade Scheme' });
        }
        if (url.match(/\/projects\/clients$/)) return json(CLIENTS);
        if (url.match(/\/projects(\?|$)/)) return json([PROJECT]);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderList();

    // The project code cell links to the project's detail (the drill-through).
    const codeLink = await screen.findByRole('link', { name: 'BRI-001' });
    expect(codeLink.getAttribute('href')).toBe('/m/projects/11');

    // Open the edit modal from the row action.
    fireEvent.click(await screen.findByRole('button', { name: /edit BRI-001/i }));
    const dialog = await screen.findByRole('dialog');

    // Code + client are shown read-only (identity, not editable).
    const codeField = within(dialog).getByLabelText(/^Code/i) as HTMLInputElement;
    expect(codeField.value).toBe('BRI-001');
    expect(codeField).toHaveAttribute('readonly');

    // Fix the name typo and save.
    const nameField = within(dialog).getByLabelText(/^Name/i);
    fireEvent.change(nameField, { target: { value: 'Q3 Trade Scheme' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(patchedBody).not.toBeNull());
    expect(patchedUrl).toMatch(/\/projects\/11$/);
    const body = patchedBody as Record<string, unknown>;
    expect(body.name).toBe('Q3 Trade Scheme');
    // Editable fields only — never code / client_id.
    expect(body).not.toHaveProperty('code');
    expect(body).not.toHaveProperty('client_id');
    // Modal closes on success.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('surfaces a backend 422 and keeps the edit modal open', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.match(/\/projects\/11$/) && method === 'PATCH') {
          return json({ detail: 'name is required' }, 422);
        }
        if (url.match(/\/projects\/clients$/)) return json(CLIENTS);
        if (url.match(/\/projects(\?|$)/)) return json([PROJECT]);
        if (url.endsWith('/me')) return meResponse('MANAGE');
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    renderList();
    fireEvent.click(await screen.findByRole('button', { name: /edit BRI-001/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Name/i), { target: { value: 'Something' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i }));

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('does NOT show the Edit affordance for an OPERATE (non-MANAGE) user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.match(/\/projects\/clients$/)) return json(CLIENTS);
        if (url.match(/\/projects(\?|$)/)) return json([PROJECT]);
        if (url.endsWith('/me')) return meResponse('OPERATE');
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderList();
    // The project row renders…
    expect(await screen.findByText('Q3 Trate Scheme')).toBeInTheDocument();
    // …but a non-MANAGE user gets no Edit button.
    expect(screen.queryByRole('button', { name: /edit BRI-001/i })).not.toBeInTheDocument();
  });
});
