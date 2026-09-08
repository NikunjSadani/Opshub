import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { FinanceModule } from './FinanceModule';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Pick an option from a SearchableSelect combobox (Client is searchable): focus to open the
 * listbox, then mousedown the matching option (SearchableSelect commits on mousedown). */
async function pickCombo(labelRe: RegExp, optionRe: RegExp) {
  const combo = screen.getByRole('combobox', { name: labelRe });
  // The picker is disabled until its options query resolves — wait, else focus is a no-op
  // and the listbox never opens.
  await waitFor(() => expect(combo).toBeEnabled());
  fireEvent.focus(combo);
  const opt = await screen.findByRole('option', { name: optionRe });
  fireEvent.mouseDown(opt);
}

/** `GET /me` payload — grants VIEW on the finance module (all P&L reads are VIEW). */
function meResponse(): Response {
  return json({
    id: 1,
    email: 'admin@example.com',
    name: 'Ada Admin',
    role_id: 1,
    role_name: 'Administrator',
    is_administrator: true,
    module_levels: { finance: 'VIEW' },
    platform: [],
  });
}

/** Clients for the filter dropdown (backend Client[] projection). */
const CLIENTS = [
  { id: '1', name: 'Britannia', code: 'BRI', pan: null, credit_terms_days: null, active: true },
  { id: '2', name: 'ITC', code: 'ITC', pan: null, credit_terms_days: null, active: true },
];

/** Consolidated rollup: grand totals + a general-overhead bucket + an unattributed bucket. */
const CONSOLIDATED = {
  totals: { revenue_paise: 22200000, cost_paise: 8800000, margin_paise: 13400000 },
  general_bucket: { revenue_paise: 0, cost_paise: 2500000, margin_paise: -2500000 },
  unattributed: { revenue_paise: 1500000, cost_paise: 300000, margin_paise: 1200000 },
  projects: [],
};

/** Per-project rows: a profit, a LOSS (negative margin), and a zero-revenue (null margin%). */
const PROJECT_ROWS = [
  {
    project_id: 10,
    project_code: 'BRI-001',
    project_name: 'Alpha',
    client_name: 'Britannia',
    revenue_paise: 10000000,
    cost_paise: 6000000,
    margin_paise: 4000000,
    margin_pct: 40.0,
  },
  {
    project_id: 11,
    project_code: 'BRI-002',
    project_name: 'Beta',
    client_name: 'Britannia',
    revenue_paise: 5000000,
    cost_paise: 8000000,
    margin_paise: -3000000,
    margin_pct: -60.0,
  },
  {
    project_id: 12,
    project_code: 'ITC-001',
    project_name: 'Gamma',
    client_name: 'ITC',
    revenue_paise: 0,
    cost_paise: 2000000,
    margin_paise: -2000000,
    margin_pct: null,
  },
];

/** Wrap a subtree with the app's providers. */
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

/**
 * Stub fetch for the finance screens. Records every `/finance/pnl/projects` URL so
 * a test can assert the filter query params, and returns the mock rollups.
 */
function stubFetch(): { projectUrls: string[] } {
  const projectUrls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/finance/pnl/consolidated')) return json(CONSOLIDATED);
      if (url.includes('/finance/pnl/projects')) {
        projectUrls.push(url);
        return json(PROJECT_ROWS);
      }
      if (url.includes('/projects/clients')) return json(CLIENTS);
      if (url.endsWith('/me')) return meResponse();
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  return { projectUrls };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('FinanceModule (P&L dashboard)', () => {
  it('renders the consolidated tiles from integer paise', async () => {
    stubFetch();
    renderWithProviders(<FinanceModule />);

    // Total revenue (₹2,22,000.00) and total margin (₹1,34,000.00) from paise.
    expect(await screen.findByText('₹2,22,000.00')).toBeInTheDocument();
    expect(screen.getByText('₹1,34,000.00')).toBeInTheDocument();
  });

  it('shows the general-overhead bucket separately from the project rows', async () => {
    stubFetch();
    renderWithProviders(<FinanceModule />);

    // The callout has its own heading + its (loss) margin, rendered OUTSIDE the
    // project table (so the overhead bucket never leaks in as a project row).
    expect(await screen.findByText(/general \/ overhead/i)).toBeInTheDocument();
    const generalMargin = screen.getByText(/-₹25,000\.00/);
    expect(generalMargin.closest('table')).toBeNull();
  });

  it('shows the unattributed bucket separately when it carries money', async () => {
    stubFetch();
    renderWithProviders(<FinanceModule />);

    // The unattributed callout has its own heading + its revenue (₹15,000.00), rendered
    // OUTSIDE the project table (so a PO-less unattributed invoice never leaks in as a row).
    expect(await screen.findByText(/^Unattributed$/i)).toBeInTheDocument();
    const unattrRevenue = screen.getByText(/₹15,000\.00/);
    expect(unattrRevenue.closest('table')).toBeNull();
  });

  it('renders a negative-margin project in red with the correct sign', async () => {
    stubFetch();
    renderWithProviders(<FinanceModule />);

    // Beta is a loss: margin -3000000 paise → "-₹30,000.00", tinted red.
    const marginCell = await screen.findByText(/-₹30,000\.00/);
    expect(marginCell).toHaveClass('text-rose-600');
  });

  it('renders a null margin% as an em dash', async () => {
    stubFetch();
    renderWithProviders(<FinanceModule />);

    // Gamma has zero revenue → margin_pct is null → rendered as "—".
    const gammaRow = (await screen.findByText(/ITC-001/)).closest('tr') as HTMLElement;
    expect(within(gammaRow).getByText('—')).toBeInTheDocument();
    // A real percentage still renders for a normal project.
    const alphaRow = screen.getByText(/BRI-001/).closest('tr') as HTMLElement;
    expect(within(alphaRow).getByText('40.0%')).toBeInTheDocument();
  });

  it('drives the project query with the selected client filter', async () => {
    const { projectUrls } = stubFetch();
    renderWithProviders(<FinanceModule />);

    // Wait for the initial (unfiltered) load.
    await screen.findByText(/BRI-001/);
    expect(projectUrls.some((u) => u.includes('client_id='))).toBe(false);

    // Choosing a client re-queries with `client_id=1`.
    await pickCombo(/client/i, /Britannia/i);
    await waitFor(() =>
      expect(projectUrls.some((u) => u.includes('client_id=1'))).toBe(true),
    );
  });
});
