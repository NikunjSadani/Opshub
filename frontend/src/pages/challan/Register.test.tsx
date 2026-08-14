import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { Register } from './Register';

function renderRegister() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <Register />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Register table', () => {
  beforeEach(() => {
    // The register fetches one page of challans on mount. Return a single row
    // carrying the new `project_code` field; any other call fails loudly.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/challan/challans')) {
          return new Response(
            JSON.stringify([
              {
                id: 1,
                number: 'L/26-27/0001',
                series: 'L',
                fy: '26-27',
                challan_date: '2026-04-15',
                project_code: 'BRI-001',
                consignee_name: 'Acme Distributors',
                ship_to_state: 'Maharashtra',
                eway_required: false,
                total_paise: 123450,
                status: 'ISSUED',
                pdf_file_id: null,
              },
            ]),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders a Project ID column showing project_code alongside the consignee name', async () => {
    renderRegister();

    // The new column header is present.
    expect(await screen.findByText('Project ID')).toBeInTheDocument();
    // The row renders the project_code and the consignee name (no brand).
    expect(await screen.findByText('BRI-001')).toBeInTheDocument();
    expect(screen.getByText('Acme Distributors')).toBeInTheDocument();
  });
});
