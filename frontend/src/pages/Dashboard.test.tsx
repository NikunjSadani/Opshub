import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../auth/AuthProvider';
import { ToastProvider } from '../ui';
import { Dashboard } from './Dashboard';

function renderDashboard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <Dashboard />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Dashboard modules error retry', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders a retry affordance on load failure and recovers on retry', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/modules')) {
          calls += 1;
          if (calls === 1) {
            return new Response(JSON.stringify({ detail: 'boom' }), {
              status: 500,
              headers: { 'content-type': 'application/json' },
            });
          }
          return new Response(
            JSON.stringify([
              { key: 'document_automation', title: 'Document Automation', nav_group: 'Operations', coming_soon: false },
            ]),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderDashboard();

    // The shared ErrorState with its "Try again" button is shown (not bare text).
    const retry = await screen.findByRole('button', { name: /try again/i });
    expect(retry).toBeInTheDocument();

    // Retrying refetches and the module tile renders.
    fireEvent.click(retry);
    expect(await screen.findByText('Document Automation')).toBeInTheDocument();
  });
});
