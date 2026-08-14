import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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

  it('shows a non-blocking warnings notice on a VALIDATED-with-warnings batch and keeps Generate enabled', async () => {
    // Override the mount stub: the POST upload resolves to VALIDATED but carries
    // warnings (message + error_report_file_id). GET recent-batches stays empty.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/challan/batches') && method === 'POST') {
          return new Response(
            JSON.stringify({
              id: 42,
              status: 'VALIDATED',
              challan_count: 3,
              line_count: 7,
              message: '2 warning(s)',
              error_report_file_id: 99,
              zip_file_id: null,
              merged_pdf_file_id: null,
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        if (url.includes('/challan/batches')) {
          return new Response('[]', {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderNewChallan();

    const fileInput = document.getElementById('challan-file') as HTMLInputElement;
    const file = new File(['data'], 'challans.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    fireEvent.change(fileInput, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /upload & validate/i }));

    // The amber warnings notice appears inside the validated block. Match a
    // phrase unique to the notice (the sr-only live region also says
    // "non-blocking", so a looser match would find two elements).
    expect(await screen.findByText(/Review before generating/i)).toBeInTheDocument();
    expect(screen.getByText('2 warning(s)')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /download warnings report/i }),
    ).toBeInTheDocument();

    // Warnings do NOT block: Step 3's Generate button is enabled.
    const generate = screen.getByRole('button', { name: /generate 3 challans/i });
    expect(generate).toBeEnabled();
  });
});
