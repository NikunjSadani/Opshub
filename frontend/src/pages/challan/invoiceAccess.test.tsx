import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider, MockAuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { InvoiceAccess } from './InvoiceAccess';
import { ChallanModule } from './ChallanModule';

/**
 * Build the `GET /me` payload for the acting dev user (read off the `X-Dev-Uid`
 * header the mock provider sends). dev-admin/dev-manager get MANAGE on the
 * document_automation module (so the Invoice Access tab shows); everyone else
 * gets VIEW.
 */
function meResponse(init?: RequestInit): Response {
  const headers = (init?.headers ?? {}) as Record<string, string>;
  const uid = headers['X-Dev-Uid'] ?? 'dev-admin';
  const manage = uid === 'dev-admin' || uid === 'dev-manager';
  return new Response(
    JSON.stringify({
      id: 1,
      email: 'test@example.com',
      name: 'Test User',
      role_id: 1,
      role_name: manage ? 'Administrator' : 'Viewer',
      is_administrator: uid === 'dev-admin',
      module_levels: { document_automation: manage ? 'MANAGE' : 'VIEW' },
      platform: uid === 'dev-admin' ? ['iam', 'settings'] : [],
    }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  );
}

const SUMMARY = {
  total_pin_entries: 140,
  total_views: 97,
  total_not_available: 6,
  total_failed: 37,
  approx_viewers: 58,
  by_outcome: { VIEWED: 97, WRONG_PIN: 25, NOT_AVAILABLE: 6, RATE_LIMITED: 4, NO_PIN: 8 },
  by_client: [
    {
      client_id: 11,
      client_name: 'Britannia',
      client_code: 'BRI',
      pin_entries: 90,
      views: 70,
      failed: 20,
      approx_viewers: 40,
    },
  ],
  trend: [
    { date: '2026-08-20', views: 30, failed: 10 },
    { date: '2026-08-21', views: 40, failed: 12 },
  ],
};

const RECENT = [
  {
    challan_number: 'L/26-27/0007',
    client_name: 'Britannia',
    accessed_at: '2026-08-26T10:15:00Z',
    outcome: 'VIEWED',
  },
  {
    challan_number: 'L/26-27/0008',
    client_name: 'Britannia',
    accessed_at: '2026-08-26T11:00:00Z',
    outcome: 'WRONG_PIN',
  },
];

const json = (data: unknown) =>
  new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

function renderInvoiceAccess() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <InvoiceAccess />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Render the whole module shell at its mount path so the tab bar is exercised. */
function renderModule(children: (node: ReactNode) => ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/m/document_automation']}>
        {children(
          <ToastProvider>
            <Routes>
              <Route path="/m/document_automation/*" element={<ChallanModule />} />
            </Routes>
          </ToastProvider>,
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('InvoiceAccess dashboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the stat tiles, a by-client row, and a recent-access outcome badge', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/challan/invoice-access/summary')) return json(SUMMARY);
        if (url.includes('/challan/invoice-access/recent')) return json(RECENT);
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderInvoiceAccess();

    // Stat tiles render their numbers (PIN entries / viewed / failed / ~viewers).
    expect(await screen.findByText('140')).toBeInTheDocument();
    expect(screen.getByText('97')).toBeInTheDocument();
    expect(screen.getByText('37')).toBeInTheDocument();
    expect(screen.getByText('58')).toBeInTheDocument();

    // The by-client row renders name + code ('Britannia' also appears in the
    // recent rows, so assert the unique code and the row's PIN-entry count).
    expect(screen.getAllByText('Britannia').length).toBeGreaterThan(0);
    expect(screen.getByText('BRI')).toBeInTheDocument();
    expect(screen.getByText('90')).toBeInTheDocument();

    // A recent row renders its challan number and a coloured outcome badge.
    // ('Viewed' also names the by-client column header, so assert the unique
    // 'Wrong PIN' badge plus the second recent challan number.)
    expect(await screen.findByText('L/26-27/0007')).toBeInTheDocument();
    expect(screen.getByText('L/26-27/0008')).toBeInTheDocument();
    expect(screen.getByText('Wrong PIN')).toBeInTheDocument();
  });

  it('shows an empty state when nothing has been accessed', async () => {
    const EMPTY = {
      total_pin_entries: 0,
      total_views: 0,
      total_not_available: 0,
      total_failed: 0,
      approx_viewers: 0,
      by_outcome: { VIEWED: 0, WRONG_PIN: 0, NOT_AVAILABLE: 0, RATE_LIMITED: 0, NO_PIN: 0 },
      by_client: [],
      trend: [],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/challan/invoice-access/summary')) return json(EMPTY);
        if (url.includes('/challan/invoice-access/recent')) return json([]);
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderInvoiceAccess();

    expect(await screen.findByText(/no invoice access yet/i)).toBeInTheDocument();
  });
});

describe('Invoice Access tab gating', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows the Invoice Access tab to a MANAGE user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // The index route is Overview, which loads the register summary.
        if (url.includes('/challan/summary')) {
          return json({
            issued_count: 0,
            void_count: 0,
            eway_count: 0,
            valued_count: 0,
            total_value_paise: 0,
            by_series: [],
          });
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    // Default AuthProvider acts as dev-admin (MANAGE).
    renderModule((node) => <AuthProvider>{node}</AuthProvider>);

    expect(await screen.findByRole('link', { name: /invoice access/i })).toBeInTheDocument();
  });

  it('hides the Invoice Access tab from a VIEW user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/challan/summary')) {
          return json({
            issued_count: 0,
            void_count: 0,
            eway_count: 0,
            valued_count: 0,
            total_value_paise: 0,
            by_series: [],
          });
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    // A viewer (VIEW level) never gets the MANAGE-gated tab.
    renderModule((node) => (
      <MockAuthProvider initialUid="dev-viewer">{node}</MockAuthProvider>
    ));

    // The Overview tab always shows; wait for it so /me has resolved.
    expect(await screen.findByRole('link', { name: /overview/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /invoice access/i })).not.toBeInTheDocument();
  });
});
