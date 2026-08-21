import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useApi } from './client';

/**
 * Typed contracts + React Query hook for the Action Center module (module key
 * `action_center`, VIEW-gated). Mirrors the backend `GET /action-center` route,
 * which returns three nested lists of items needing attention plus their counts.
 * All paths are relative to `/api/v1` (added by `useApi()`). Money is integer
 * PAISE on the wire; quantities arrive as decimal STRINGS.
 */

/** A PO whose procurement follow-up date falls within the horizon. */
export interface ProcurementItem {
  po_id: number;
  po_number: string;
  client_name: string | null;
  project_code: string | null;
  /** Plain YYYY-MM-DD (backend `date`). */
  expected_procurement_date: string;
  /** Days from today until the date; NEGATIVE when it is already past. */
  days_until: number;
}

/** A PO with delivered-but-uninvoiced quantity awaiting a client invoice. */
export interface InvoicingDueItem {
  po_id: number;
  po_number: string;
  client_name: string | null;
  project_code: string | null;
  /** Uninvoiced quantity as a decimal STRING (e.g. "12.000"). */
  uninvoiced_qty: string;
  /** PAISE. */
  uninvoiced_value_paise: number;
}

/** An overdue client receivable (AR invoice past its due date). */
export interface ArOverdueItem {
  invoice_id: number;
  invoice_number: string;
  client_name: string | null;
  /** PAISE still outstanding. */
  outstanding_paise: number;
  /** Plain YYYY-MM-DD (backend `date`). */
  due_date: string;
  /** Whole days past the due date. */
  days_overdue: number;
  /** Aging bucket label, e.g. "0-30" / "31-60" / "61-90" / "90+". */
  aging_bucket: string;
}

/** Per-category counts (mirror the list lengths; used by the tiles). */
export interface ActionCenterCounts {
  procurement: number;
  invoicing_due: number;
  ar_overdue: number;
}

/** The full Action Center payload (backend GET /action-center). */
export interface ActionCenter {
  procurement: ProcurementItem[];
  invoicing_due: InvoicingDueItem[];
  ar_overdue: ArOverdueItem[];
  counts: ActionCenterCounts;
}

/** Default look-ahead window (days) for procurement follow-ups. */
export const DEFAULT_HORIZON_DAYS = 15;

// --- query keys ---------------------------------------------------------------

export const actionCenterKeys = {
  root: ['action_center'] as const,
  dashboard: (horizonDays: number) => ['action_center', 'dashboard', horizonDays] as const,
};

// --- queries ------------------------------------------------------------------

/**
 * The "what needs attention" rollup: procurement follow-ups due within
 * `horizonDays`, invoicing-due POs, and overdue receivables, plus their counts.
 */
export function useActionCenter(
  horizonDays: number = DEFAULT_HORIZON_DAYS,
): UseQueryResult<ActionCenter, Error> {
  const { get } = useApi();
  return useQuery<ActionCenter, Error>({
    queryKey: actionCenterKeys.dashboard(horizonDays),
    queryFn: ({ signal }) =>
      get<ActionCenter>(`/action-center?horizon_days=${horizonDays}`, signal),
  });
}
