import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useApi } from './client';

/**
 * Typed contracts + React Query hooks for the Invoice Access dashboard — the
 * public challan-QR invoice-viewer's access log. Mirrors the backend
 * `/challan/invoice-access/*` endpoints. All paths are relative to `/api/v1`
 * (added by `useApi()`).
 *
 * The public invoice viewer has no login, so per-person identity is unknowable;
 * "approx viewers" is a coarse device/IP-based estimate, never an exact headcount.
 */

/** The recorded result of one public invoice-viewer access attempt. */
export type AccessOutcome = 'VIEWED' | 'WRONG_PIN' | 'NOT_AVAILABLE' | 'RATE_LIMITED' | 'NO_PIN';

/** Per-client rollup row (backend `by_client`). */
export interface InvoiceAccessClientRow {
  client_id: number;
  client_name: string | null;
  client_code: string | null;
  pin_entries: number;
  views: number;
  failed: number;
  approx_viewers: number;
}

/** One point on the daily trend (backend `trend`). */
export interface InvoiceAccessTrendPoint {
  /** YYYY-MM-DD. */
  date: string;
  views: number;
  failed: number;
}

/** Aggregate summary over the access log (backend InvoiceAccessSummaryOut). */
export interface InvoiceAccessSummary {
  total_pin_entries: number;
  total_views: number;
  total_not_available: number;
  total_failed: number;
  /** Coarse device/IP-based estimate — NOT an exact unique-person count. */
  approx_viewers: number;
  by_outcome: Record<AccessOutcome, number>;
  by_client: InvoiceAccessClientRow[];
  trend: InvoiceAccessTrendPoint[];
}

/** One recent access-log entry (backend `recent` row). */
export interface InvoiceAccessRecentRow {
  challan_number: string;
  client_name: string | null;
  /** ISO timestamp of the access. */
  accessed_at: string;
  outcome: AccessOutcome;
}

/** Date-range (+ optional client) filter for the summary. */
export interface InvoiceAccessFilters {
  /** Inclusive lower bound (YYYY-MM-DD). */
  from?: string;
  /** Inclusive upper bound (YYYY-MM-DD). */
  to?: string;
  /** Restrict to a single client. */
  client_id?: number | null;
}

/** Params for the recent-access list. */
export interface InvoiceAccessRecentParams {
  limit?: number;
  client_id?: number | null;
}

// --------------------------------------------------------------- query keys
export const invoiceAccessKeys = {
  summary: (filters: InvoiceAccessFilters) => ['invoice-access', 'summary', filters] as const,
  recent: (params: InvoiceAccessRecentParams) => ['invoice-access', 'recent', params] as const,
};

/**
 * Build a `?from=&to=&client_id=` query string for the summary, appending only
 * non-empty (trimmed) params. Exported + pure so it can be unit-tested.
 */
export function buildInvoiceAccessSummaryQuery(filters: InvoiceAccessFilters): string {
  const params = new URLSearchParams();
  if (filters.from?.trim()) params.set('from', filters.from.trim());
  if (filters.to?.trim()) params.set('to', filters.to.trim());
  if (filters.client_id != null) params.set('client_id', String(filters.client_id));
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

/** Build a `?limit=&client_id=` query string for the recent-access list. */
export function buildInvoiceAccessRecentQuery(params: InvoiceAccessRecentParams): string {
  const p = new URLSearchParams();
  if (params.limit != null) p.set('limit', String(params.limit));
  if (params.client_id != null) p.set('client_id', String(params.client_id));
  const qs = p.toString();
  return qs ? `?${qs}` : '';
}

// ------------------------------------------------------------------ queries

/** Aggregate invoice-access summary (totals + per-outcome + per-client + trend). */
export function useInvoiceAccessSummary(
  filters: InvoiceAccessFilters,
): UseQueryResult<InvoiceAccessSummary, Error> {
  const { get } = useApi();
  return useQuery<InvoiceAccessSummary, Error>({
    queryKey: invoiceAccessKeys.summary(filters),
    queryFn: ({ signal }) =>
      get<InvoiceAccessSummary>(
        `/challan/invoice-access/summary${buildInvoiceAccessSummaryQuery(filters)}`,
        signal,
      ),
  });
}

/** The most-recent invoice-viewer access attempts (newest first, server-ordered). */
export function useInvoiceAccessRecent(
  params: InvoiceAccessRecentParams,
): UseQueryResult<InvoiceAccessRecentRow[], Error> {
  const { get } = useApi();
  return useQuery<InvoiceAccessRecentRow[], Error>({
    queryKey: invoiceAccessKeys.recent(params),
    queryFn: ({ signal }) =>
      get<InvoiceAccessRecentRow[]>(
        `/challan/invoice-access/recent${buildInvoiceAccessRecentQuery(params)}`,
        signal,
      ),
  });
}
