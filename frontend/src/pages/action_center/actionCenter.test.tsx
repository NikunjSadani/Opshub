import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { ActionCenterModule } from './ActionCenterModule';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** `GET /me` payload the mock provider fetches on sign-in — VIEW on action_center. */
function meResponse(): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: 'Administrator',
    is_administrator: true,
    module_levels: { action_center: 'VIEW' },
    platform: [],
  });
}

/** Wrap a subtree with the app's providers (router + auth + toasts + query). */
function renderWithProviders(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>{node}</ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A populated payload matching the backend's exact nested shape. */
const FULL_PAYLOAD = {
  procurement: [
    {
      po_id: 100,
      po_number: 'PO-100',
      client_name: 'Britannia',
      project_code: 'BRI-001',
      // A date already in the past → the row must read "5 days ago".
      expected_procurement_date: '2026-08-16',
      days_until: -5,
    },
    {
      po_id: 101,
      po_number: 'PO-101',
      client_name: 'Deoleo',
      project_code: 'DEO-002',
      expected_procurement_date: '2026-08-25',
      days_until: 4,
    },
  ],
  invoicing_due: [
    {
      po_id: 200,
      po_number: 'PO-200',
      client_name: 'Britannia',
      project_code: 'BRI-001',
      uninvoiced_qty: '12.000',
      // 1500000 paise -> ₹15,000.00
      uninvoiced_value_paise: 1500000,
    },
  ],
  ar_overdue: [
    {
      invoice_id: 9,
      invoice_number: 'INV-9',
      client_name: 'Britannia',
      outstanding_paise: 250000,
      due_date: '2026-07-10',
      days_overdue: 42,
      aging_bucket: '31-60',
    },
  ],
  counts: { procurement: 2, invoicing_due: 1, ar_overdue: 1 },
};

const EMPTY_PAYLOAD = {
  procurement: [],
  invoicing_due: [],
  ar_overdue: [],
  counts: { procurement: 0, invoicing_due: 0, ar_overdue: 0 },
};

function stub(payload: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/action-center')) return json(payload);
      if (url.endsWith('/me')) return meResponse();
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ActionCenterModule', () => {
  it('renders the three count tiles from the payload counts', async () => {
    stub(FULL_PAYLOAD);
    renderWithProviders(<ActionCenterModule />);

    // The tile label collides with the section header text, so take the tile
    // (first in the DOM) and read its count within that Card.
    const [procTile] = await screen.findAllByText('Procurement follow-ups');
    expect(within(procTile.parentElement as HTMLElement).getByText('2')).toBeInTheDocument();

    const [invTile] = screen.getAllByText('Invoicing due');
    expect(within(invTile.parentElement as HTMLElement).getByText('1')).toBeInTheDocument();

    const [arTile] = screen.getAllByText('Overdue receivables');
    expect(within(arTile.parentElement as HTMLElement).getByText('1')).toBeInTheDocument();
  });

  it('shows "N days ago" for a past procurement follow-up date', async () => {
    stub(FULL_PAYLOAD);
    renderWithProviders(<ActionCenterModule />);

    expect(await screen.findByText('5 days ago')).toBeInTheDocument();
    // The upcoming one reads the amber "in N days" form.
    expect(screen.getByText('in 4 days')).toBeInTheDocument();
  });

  it('renders an invoicing-due row with its rupee value', async () => {
    stub(FULL_PAYLOAD);
    renderWithProviders(<ActionCenterModule />);

    const cell = await screen.findByText('PO-200');
    const row = cell.closest('tr') as HTMLElement;
    // 1500000 paise -> ₹15,000.00, and the uninvoiced qty string is shown as-is.
    expect(within(row).getByText('₹15,000.00')).toBeInTheDocument();
    expect(within(row).getByText('12.000')).toBeInTheDocument();
  });

  it('renders an overdue receivable with its aging badge', async () => {
    stub(FULL_PAYLOAD);
    renderWithProviders(<ActionCenterModule />);

    const cell = await screen.findByText('INV-9');
    const row = cell.closest('tr') as HTMLElement;
    expect(within(row).getByText('31-60')).toBeInTheDocument();
    // Outstanding rendered from integer paise (₹2,500.00) and the day count.
    expect(within(row).getByText('₹2,500.00')).toBeInTheDocument();
    expect(within(row).getByText('42')).toBeInTheDocument();
  });

  it('shows an honest empty state for each category when nothing needs attention', async () => {
    stub(EMPTY_PAYLOAD);
    renderWithProviders(<ActionCenterModule />);

    // One empty panel per section (procurement / invoicing / receivables).
    const empties = await screen.findAllByText('Nothing needs attention here');
    expect(empties).toHaveLength(3);

    // The tiles still render, all reading zero.
    const [procTile] = screen.getAllByText('Procurement follow-ups');
    expect(within(procTile.parentElement as HTMLElement).getByText('0')).toBeInTheDocument();
  });
});
