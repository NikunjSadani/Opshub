import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useApi } from './client';

/**
 * Typed contracts + React Query hooks for the Finance / P&L module.
 * Mirrors the backend `finance` routes. All paths are relative to `/api/v1`
 * (added by `useApi()`). Money is integer PAISE on the wire; margin can be
 * NEGATIVE, and `margin_pct` is NULL when revenue is 0 (undefined ratio).
 */

/** A single project's P&L row (backend GET /finance/pnl/projects row). */
export interface PnlProjectRow {
  project_id: number;
  project_code: string;
  project_name: string;
  client_name: string;
  /** PAISE. */
  revenue_paise: number;
  /** PAISE. */
  cost_paise: number;
  /** PAISE, SIGNED (revenue − cost; negative = loss). */
  margin_paise: number;
  /** Margin as a percentage of revenue, or NULL when revenue is 0. */
  margin_pct: number | null;
}

/** A revenue / cost / margin triple (a project breakdown, or a consolidated bucket). */
export interface PnlBreakdown {
  /** PAISE. */
  revenue_paise: number;
  /** PAISE. */
  cost_paise: number;
  /** PAISE, SIGNED. */
  margin_paise: number;
  /** NULL when revenue is 0. Present on a single-project breakdown; absent on the
   * consolidated `totals` / `general_bucket` sub-objects, hence optional. */
  margin_pct?: number | null;
}

/**
 * The consolidated P&L rollup (backend GET /finance/pnl/consolidated):
 * grand totals, the un-attributed general-overhead bucket (shown separately from
 * project rows), and the per-project rows.
 */
export interface ConsolidatedPnl {
  totals: PnlBreakdown;
  general_bucket: PnlBreakdown;
  projects: PnlProjectRow[];
}

/** Filters for the per-project P&L list. */
export interface PnlFilters {
  client_id?: string;
  /** Inclusive lower bound (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound (YYYY-MM-DD). */
  date_to?: string;
}

/**
 * Build the `?client_id=&date_from=&date_to=` query for the per-project P&L list,
 * appending only non-empty (trimmed) params. Exported + pure so it can be
 * unit-tested and reused by the CSV export so their filter semantics stay
 * identical.
 */
export function buildPnlQuery(filters: PnlFilters): string {
  const params = new URLSearchParams();
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const financeKeys = {
  projects: (filters: PnlFilters) => ['finance', 'pnl', 'projects', filters] as const,
  project: (id: string) => ['finance', 'pnl', 'project', id] as const,
  consolidated: ['finance', 'pnl', 'consolidated'] as const,
};

// --- queries ------------------------------------------------------------------

/** Per-project P&L, filtered by client + date range. */
export function usePnlByProject(filters: PnlFilters): UseQueryResult<PnlProjectRow[], Error> {
  const { get } = useApi();
  return useQuery<PnlProjectRow[], Error>({
    queryKey: financeKeys.projects(filters),
    queryFn: ({ signal }) =>
      get<PnlProjectRow[]>(`/finance/pnl/projects${buildPnlQuery(filters)}`, signal),
  });
}

/** One project's P&L breakdown (revenue / cost / margin / margin%). */
export function useProjectPnl(id: string | null): UseQueryResult<PnlBreakdown, Error> {
  const { get } = useApi();
  return useQuery<PnlBreakdown, Error>({
    queryKey: financeKeys.project(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<PnlBreakdown>(`/finance/pnl/projects/${id}`, signal),
  });
}

/** The consolidated rollup: totals + general-overhead bucket + per-project rows. */
export function useConsolidatedPnl(): UseQueryResult<ConsolidatedPnl, Error> {
  const { get } = useApi();
  return useQuery<ConsolidatedPnl, Error>({
    queryKey: financeKeys.consolidated,
    queryFn: ({ signal }) => get<ConsolidatedPnl>('/finance/pnl/consolidated', signal),
  });
}
