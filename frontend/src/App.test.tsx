import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import App from './App';
import type { ModuleDescriptor } from './types/modules';

const MODULES: ModuleDescriptor[] = [
  { key: 'delivery-challan', title: 'Delivery Challan', nav_group: 'Modules', coming_soon: false },
  { key: 'expense-invoice', title: 'Expenses', nav_group: 'Modules', coming_soon: true },
  { key: 'user-management', title: 'User Management', nav_group: 'Platform', coming_soon: false },
];

/** The permissions payload the mock provider fetches from GET /me on sign-in. */
const ME = {
  id: 1,
  email: 'admin@example.com',
  name: 'Ada Admin',
  role_id: 1,
  role_name: 'Administrator',
  is_administrator: true,
  module_levels: {
    document_automation: 'MANAGE',
    projects: 'MANAGE',
    expense_invoice: 'MANAGE',
  },
  platform: ['iam', 'settings'],
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
  } as unknown as Response;
}

describe('OpsHub app shell', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/modules')) return jsonResponse(MODULES);
        if (url.endsWith('/api/v1/me')) return jsonResponse(ME);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders module tiles from GET /api/v1/modules', async () => {
    render(<App />);

    // The mock auth signs in as ADMIN, so we land on the Dashboard, which
    // fetches modules and renders it (appears in both the sidebar and a tile).
    const titles = await screen.findAllByText('Delivery Challan');
    expect(titles.length).toBeGreaterThan(0);

    // A coming-soon module still renders, badged as such (tile-only text).
    expect(await screen.findByText('Coming Soon')).toBeInTheDocument();

    // The signed-in user's role name (from GET /me) shows in the top bar.
    // (Scoped to the header: the dev user switcher renders its labels outside
    // the <header> banner.)
    const header = await screen.findByRole('banner');
    expect(await within(header).findByText('Administrator')).toBeInTheDocument();
  });

  it('called the modules endpoint with a bearer token', async () => {
    render(<App />);
    await screen.findAllByText('Delivery Challan');

    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>;
    const call = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith('/api/v1/modules'),
    );
    expect(call).toBeTruthy();
    const init = call?.[1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toMatch(/^Bearer /);
  });
});

describe('OpsHub app shell — access load failure', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows a global "couldn\'t load your access" state with Retry when /me fails, and recovers', async () => {
    // /me fails the first time, then succeeds — the Retry button re-runs it.
    let meCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/modules')) return jsonResponse(MODULES);
        if (url.endsWith('/api/v1/me')) {
          meCalls += 1;
          if (meCalls === 1) {
            return {
              ok: false,
              status: 500,
              statusText: 'Server Error',
              headers: new Headers({ 'content-type': 'application/json' }),
              json: async () => ({ detail: 'boom' }),
            } as unknown as Response;
          }
          return jsonResponse(ME);
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    render(<App />);

    // The shell renders a clear global load-failure state — not empty nav +
    // a misleading "no permission" page.
    expect(await screen.findByText(/couldn't load your access/i)).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: /retry/i });

    // Retrying recovers: /me succeeds, the real shell + role identity appear.
    fireEvent.click(retry);
    const header = await screen.findByRole('banner');
    expect(await within(header).findByText('Administrator')).toBeInTheDocument();
    expect(screen.queryByText(/couldn't load your access/i)).not.toBeInTheDocument();
  });
});
