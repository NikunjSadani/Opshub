import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '../../../auth/AuthProvider';
import { ToastProvider } from '../../../ui';
import { MasterData } from '../MasterData';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
  } as unknown as Response;
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={['/m/document_automation/master-data']}>
            <Routes>
              <Route path="/m/document_automation/master-data/*" element={<MasterData />} />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe('MasterData admin screen', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/masterdata/')) return jsonResponse([]);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('lands on the Consignor tab and renders its empty state', async () => {
    renderScreen();

    // Consignor is the index route: its page heading renders.
    expect(await screen.findByRole('heading', { name: 'Consignor' })).toBeInTheDocument();

    // The mocked empty list drives the explicit empty state.
    expect(await screen.findByText(/no consignors yet/i)).toBeInTheDocument();
  });
});
