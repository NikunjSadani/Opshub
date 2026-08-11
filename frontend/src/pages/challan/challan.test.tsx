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
    // No network on initial render (batch poll is disabled until a batch id
    // exists); mock fetch so any accidental call fails loudly instead of hanging.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        throw new Error(`Unexpected fetch: ${String(input)}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the upload form and the blank-template control', () => {
    renderNewChallan();

    // Upload action + file input are present, no network needed.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download blank template/i })).toBeInTheDocument();

    // The upload button is disabled until a file is chosen.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeDisabled();

    // Never called the network just to render the form.
    expect(fetch).not.toHaveBeenCalled();
  });
});
