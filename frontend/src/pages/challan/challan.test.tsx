import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { NewChallan } from './NewChallan';

function renderNewChallan() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <NewChallan />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('NewChallan upload flow', () => {
  beforeEach(() => {
    // The screen now legitimately fetches the recent-batches list on mount
    // (useBatchesQuery). Return an empty list for that call so "Recent batches"
    // renders its empty state; any OTHER call fails loudly instead of hanging.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/challan/batches')) {
          return new Response('[]', {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the upload form and the blank-template control', async () => {
    renderNewChallan();

    // Upload action + template control are present.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download template/i })).toBeInTheDocument();

    // The upload button is disabled until a file is chosen.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeDisabled();

    // Recent batches resolves to its empty state (the one mount-time fetch).
    expect(await screen.findByText(/no batches yet/i)).toBeInTheDocument();
  });
});
