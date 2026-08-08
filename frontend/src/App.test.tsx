import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import App from './App';
import type { ModuleDescriptor } from './types/modules';

const MODULES: ModuleDescriptor[] = [
  { key: 'delivery-challan', title: 'Delivery Challan', nav_group: 'Modules', coming_soon: false },
  { key: 'expense-invoice', title: 'Expense & Invoice', nav_group: 'Modules', coming_soon: true },
  { key: 'user-management', title: 'User Management', nav_group: 'Platform', coming_soon: false },
];

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

    // The signed-in user's identity shows in the top bar.
    expect(screen.getByText('ADMIN')).toBeInTheDocument();
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
