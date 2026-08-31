import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { MockAuthProvider } from '../../auth/AuthProvider';
import { RequirePlatform } from '../../auth/RequireRole';
import { ToastProvider } from '../../ui';
import { AuditModule } from './AuditModule';
import { recordLoginEvent, useRecordLoginEvent } from '../../api/audit';

/**
 * Audit & Access tests: the page renders the activity trail + login events (incl.
 * the integrity badge) from stubbed responses; a non-`iam` user is denied the
 * surface (mirrors the Users gating test); and the login ping targets
 * POST /auth/login-event.
 */

// GET /me for an admin who holds `iam` (keyed off X-Dev-Uid like RequireRole.test).
const ADMIN_ME = {
  id: 1,
  email: 'ops.admin@gifsy.in',
  name: 'Ops Admin',
  role_id: 1,
  role_name: 'Administrator',
  is_administrator: true,
  module_levels: {},
  platform: ['iam'],
};

const EVENT = {
  id: 1,
  created_at: '2026-02-20T09:00:00Z',
  action: 'user.update',
  entity: 'user',
  entity_id: 12,
  detail: { field: 'role', to: 'Operations' },
  actor: { email: 'admin@gifsy.in', name: 'Ada Admin' },
};

const LOGIN = {
  id: 1,
  occurred_at: '2026-02-21T10:30:00Z',
  ip: '203.0.113.7',
  user_agent: 'Mozilla/5.0 (Windows NT 10.0) Chrome/120',
  user: { email: 'jane@gifsy.in', name: 'Jane Doe' },
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * Stub the audit surface + /me. `me` decides whether the signed-in user holds
 * `iam` (keyed off the mock provider's X-Dev-Uid: admin holds it, anyone else
 * does not). Any unexpected call fails loudly.
 */
function stubAudit() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/me')) {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        const uid = headers['X-Dev-Uid'] ?? 'dev-admin';
        const isAdmin = uid === 'dev-admin';
        return json({ ...ADMIN_ME, platform: isAdmin ? ['iam'] : [] });
      }
      if (url.includes('/admin/audit/integrity')) {
        return json({ intact: true, entries_checked: 42, broken_at_id: null });
      }
      if (url.includes('/admin/audit/events')) {
        return json({ items: [EVENT], has_more: false });
      }
      if (url.includes('/admin/audit/logins')) {
        return json({ items: [LOGIN], has_more: false });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
}

/** Render a node at `path` under the app's providers (mock auth as `uid`). */
function renderAt(path: string, node: ReactNode, uid = 'dev-admin') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <MockAuthProvider initialUid={uid}>
          <ToastProvider>
            <Routes>
              <Route path="/admin/audit/*" element={node} />
            </Routes>
          </ToastProvider>
        </MockAuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AuditModule — Activity tab', () => {
  it('renders audit events and the green integrity badge', async () => {
    stubAudit();
    renderAt('/admin/audit', <AuditModule />);

    // The event row: action, entity (+id), and the resolved actor.
    expect(await screen.findByText('user.update')).toBeInTheDocument();
    expect(screen.getByText('user')).toBeInTheDocument();
    expect(screen.getByText('#12')).toBeInTheDocument();
    expect(screen.getByText('Ada Admin')).toBeInTheDocument();

    // The tamper-evidence badge reads intact (green).
    expect(await screen.findByText(/audit trail intact/i)).toBeInTheDocument();
  });

  it('shows a red badge when the audit trail integrity is broken', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/me')) return json(ADMIN_ME);
        if (url.includes('/admin/audit/integrity')) {
          return json({ intact: false, entries_checked: 10, broken_at_id: 7 });
        }
        if (url.includes('/admin/audit/events')) return json({ items: [], has_more: false });
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
    renderAt('/admin/audit', <AuditModule />);

    expect(await screen.findByText(/audit trail broken/i)).toBeInTheDocument();
    // Empty result renders the empty state, not a spinner forever.
    expect(await screen.findByText(/no audit events found/i)).toBeInTheDocument();
  });
});

describe('AuditModule — Logins tab', () => {
  it('renders login events (user, IP, device)', async () => {
    stubAudit();
    renderAt('/admin/audit/logins', <AuditModule />);

    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
    expect(screen.getByText('jane@gifsy.in')).toBeInTheDocument();
    expect(screen.getByText('203.0.113.7')).toBeInTheDocument();
    expect(screen.getByText(/Chrome\/120/)).toBeInTheDocument();
  });
});

describe('AuditModule — access gating', () => {
  it('renders for an admin holding the iam permission', async () => {
    stubAudit();
    renderAt(
      '/admin/audit',
      <RequirePlatform perm="iam">
        <AuditModule />
      </RequirePlatform>,
      'dev-admin',
    );
    expect(await screen.findByText('Audit & Access')).toBeInTheDocument();
  });

  it('denies a non-iam (viewer) user — shows Not authorized, never the report', async () => {
    stubAudit();
    renderAt(
      '/admin/audit',
      <RequirePlatform perm="iam">
        <AuditModule />
      </RequirePlatform>,
      'dev-viewer',
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Not authorized');
    expect(alert).toHaveTextContent('iam');
    // The report surface never renders for a denied user.
    expect(screen.queryByText('Audit & Access')).not.toBeInTheDocument();
  });
});

describe('login-event ping', () => {
  it('recordLoginEvent POSTs to /auth/login-event with the bearer token', async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);

    await recordLoginEvent('tok-abc');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toBe('/api/v1/auth/login-event');
    expect((init.method ?? 'GET').toUpperCase()).toBe('POST');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tok-abc');
  });

  it('the useRecordLoginEvent hook posts to /auth/login-event', async () => {
    let posted: { url: string; method: string } | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/auth/login-event') && method === 'POST') {
          posted = { url, method };
          return new Response(null, { status: 204 });
        }
        if (url.includes('/me')) return json(ADMIN_ME);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    function Pinger() {
      const ping = useRecordLoginEvent();
      return (
        <button type="button" onClick={() => ping.mutate()}>
          ping
        </button>
      );
    }

    render(
      <QueryClientProvider client={new QueryClient()}>
        <MockAuthProvider>
          <Pinger />
        </MockAuthProvider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: /ping/i }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual({ url: '/api/v1/auth/login-event', method: 'POST' });
  });
});
