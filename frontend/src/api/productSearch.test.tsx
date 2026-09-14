import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider } from '../auth/AuthProvider';
import { useProductSearch } from './purchaseOrders';

/**
 * Verifies the picker-curation threading in `useProductSearch(query, projectId)`:
 * a non-empty projectId is appended as `project_id` (so the backend can return a
 * project's TAGGED products), and it is omitted entirely when unset (full catalogue).
 */

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function meResponse(): Response {
  return json({
    id: 1,
    email: 'op@example.com',
    name: 'Op',
    role_id: 1,
    role_name: 'Sales Operator',
    is_administrator: false,
    module_levels: { sales_orders: 'OPERATE' },
    platform: [],
  });
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <AuthProvider>{children}</AuthProvider>
      </QueryClientProvider>
    );
  };
}

/** The `/products?...` URLs the hook actually requested. */
function productCalls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/products?'));
}

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/me')) return meResponse();
    if (url.includes('/products?')) return json([]);
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useProductSearch project scoping', () => {
  it('appends project_id (alongside active + limit) when a project is set', async () => {
    const fetchMock = stubFetch();
    renderHook(() => useProductSearch('', '11'), { wrapper: makeWrapper() });

    await waitFor(() => expect(productCalls(fetchMock).length).toBeGreaterThan(0));
    const url = productCalls(fetchMock)[0];
    expect(url).toContain('project_id=11');
    expect(url).toContain('active=true');
    expect(url).toContain('limit=200');
  });

  it('omits project_id when no project is set', async () => {
    const fetchMock = stubFetch();
    renderHook(() => useProductSearch(''), { wrapper: makeWrapper() });

    await waitFor(() => expect(productCalls(fetchMock).length).toBeGreaterThan(0));
    const url = productCalls(fetchMock)[0];
    expect(url).not.toContain('project_id');
  });
});
