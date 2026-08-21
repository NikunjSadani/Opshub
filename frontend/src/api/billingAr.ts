import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Billing Receivables + Advances slice
 * (module key `billing`). Mirrors the backend app/modules/billing/ar_routes.py. All
 * paths are relative to `/api/v1` (added by `useApi()`).
 *
 * MONEY is integer PAISE on the wire — operator inputs are in rupees and converted
 * with {@link rupeesToPaise} before being sent (the single ₹→paise seam, mirroring
 * `rupees()` on the read path).
 *
 * Ids here are kept as NUMBERS (unlike projects/purchaseOrders which use strings):
 * this is a money surface where ids are echoed back into POST bodies typed `int` and
 * matched against numeric `client_id`/`advance_id` fields, so numbers are the correct
 * representation. `client_id` still crosses the `<select>` boundary as a string and is
 * reconciled by `String()`-keying against the clients list for the display name.
 */

// --- status / aging unions ----------------------------------------------------

/** An invoice's AR status (backend derives PAID / PART_PAID / UNPAID). */
export type ArStatus = 'PAID' | 'PART_PAID' | 'UNPAID';

/** All AR statuses, in display order (used by the register status filter). */
export const AR_STATUSES: readonly ArStatus[] = ['UNPAID', 'PART_PAID', 'PAID'];

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Sentence-case labels + badge tones for an AR status. */
export const AR_STATUS_LABEL: Record<ArStatus, string> = {
  PAID: 'Paid',
  PART_PAID: 'Part-paid',
  UNPAID: 'Unpaid',
};

export const AR_STATUS_TONE: Record<ArStatus, Tone> = {
  PAID: 'green',
  PART_PAID: 'amber',
  UNPAID: 'slate',
};

/** Aging bucket string the backend emits, or null when nothing is outstanding. */
export type AgingBucket = '0-30' | '31-60' | '61-90' | '90+';

// --- money seam ---------------------------------------------------------------

/**
 * Parse an operator-typed rupee string into integer PAISE, or null when it is not a
 * valid amount. Accepts an optional ₹, thousands separators, surrounding spaces, and
 * up to two decimals. The backend takes money as integer paise, so this is the single
 * ₹→paise seam for the billing forms.
 */
export function rupeesToPaise(input: string): number | null {
  const cleaned = input.replace(/[₹,\s]/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  return Math.round(parseFloat(cleaned) * 100);
}

// --- DTOs ---------------------------------------------------------------------

/** One AR register row (backend `InvoiceAROut`). Money is PAISE. */
export interface ArRow {
  invoice_id: number;
  client_id: number;
  invoice_number: string;
  /** ISO date string, or null. */
  invoice_date: string | null;
  /** ISO date string, or null. */
  due_date: string | null;
  grand_total_paise: number;
  credited_paise: number;
  paid_paise: number;
  applied_paise: number;
  outstanding_paise: number;
  status: ArStatus;
  overdue: boolean;
  aging_bucket: AgingBucket | null;
}

/** A recorded payment against an invoice (backend `PaymentOut`). */
export interface Payment {
  id: number;
  client_id: number;
  invoice_id: number;
  amount_paise: number;
  received_on: string;
  mode: string | null;
  reference: string | null;
  note: string | null;
  created_at: string;
}

/** An advance→invoice application (backend `ApplicationOut`). */
export interface Application {
  id: number;
  advance_id: number;
  invoice_id: number;
  amount_paise: number;
  created_at: string;
}

/** One invoice's full AR detail (backend `InvoiceARDetailOut`). */
export interface InvoiceArDetail extends ArRow {
  payments: Payment[];
  applied_advances: Application[];
}

/** A client advance/deposit with its applied/remaining split (backend `AdvanceOut`). */
export interface Advance {
  advance_id: number;
  client_id: number;
  po_id: number | null;
  amount_paise: number;
  applied_paise: number;
  remaining_paise: number;
  received_on: string;
  mode: string | null;
  reference: string | null;
  note: string | null;
}

/** One FIFO proposal line for applying advances to an invoice (backend `SuggestionOut`). */
export interface AdvanceSuggestion {
  advance_id: number;
  amount_paise: number;
  advance_remaining_paise: number;
  received_on: string;
  reference: string | null;
}

// --- request bodies -----------------------------------------------------------

export interface PaymentInput {
  invoice_id: number;
  amount_paise: number;
  /** YYYY-MM-DD. */
  received_on?: string;
  mode?: string;
  reference?: string;
  note?: string;
}

export interface AdvanceInput {
  client_id: number;
  amount_paise: number;
  /** YYYY-MM-DD. */
  received_on?: string;
  po_id?: number;
  mode?: string;
  reference?: string;
  note?: string;
}

export interface ApplicationInput {
  advance_id: number;
  invoice_id: number;
  amount_paise: number;
}

// --- filters + query-string builder (pure, unit-testable) ---------------------

export interface ArFilters {
  client_id?: string;
  status?: ArStatus | '';
  /** When true, only overdue invoices. */
  overdue?: boolean;
}

/**
 * Build a `?client_id=&status=&overdue=` query for the AR register, appending only
 * meaningful params (`overdue` only when explicitly on).
 */
export function buildArQuery(filters: ArFilters): string {
  const params = new URLSearchParams();
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.overdue) params.set('overdue', 'true');
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const arKeys = {
  register: (filters: ArFilters) => ['billing', 'ar', 'register', filters] as const,
  detail: (id: number | string) => ['billing', 'ar', 'detail', String(id)] as const,
  advances: (clientId: string) => ['billing', 'advances', clientId] as const,
  suggestion: (id: number | string) => ['billing', 'ar', 'suggestion', String(id)] as const,
};

/** Invalidate every AR + advances read after a money mutation (register, all details,
 *  all advance lists, all suggestions). Broad-but-cheap: keeps the tracker, the open
 *  invoice detail, and the advances page all consistent after a single write. */
function invalidateAll(qc: ReturnType<typeof useQueryClient>): void {
  void qc.invalidateQueries({ queryKey: ['billing', 'ar'] });
  void qc.invalidateQueries({ queryKey: ['billing', 'advances'] });
}

// --- queries ------------------------------------------------------------------

/** The AR tracker, filtered by client / status / overdue (VIEW). */
export function useArQuery(filters: ArFilters): UseQueryResult<ArRow[], Error> {
  const { get } = useApi();
  return useQuery<ArRow[], Error>({
    queryKey: arKeys.register(filters),
    queryFn: ({ signal }) => get<ArRow[]>(`/billing/ar${buildArQuery(filters)}`, signal),
  });
}

/** One invoice's AR detail incl. its payments + applied advances (VIEW). */
export function useInvoiceArQuery(id: number | null): UseQueryResult<InvoiceArDetail, Error> {
  const { get } = useApi();
  return useQuery<InvoiceArDetail, Error>({
    queryKey: arKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<InvoiceArDetail>(`/billing/invoices/${id}/ar`, signal),
  });
}

/** Client advances with remaining, optionally scoped to one client (VIEW). */
export function useAdvancesQuery(clientId: string): UseQueryResult<Advance[], Error> {
  const { get } = useApi();
  const qs = clientId.trim() ? `?client_id=${encodeURIComponent(clientId.trim())}` : '';
  return useQuery<Advance[], Error>({
    queryKey: arKeys.advances(clientId),
    queryFn: ({ signal }) => get<Advance[]>(`/billing/advances${qs}`, signal),
  });
}

/** The FIFO advance-application proposal for an invoice (VIEW). */
export function useAdvanceSuggestion(
  invoiceId: number | null,
): UseQueryResult<AdvanceSuggestion[], Error> {
  const { get } = useApi();
  return useQuery<AdvanceSuggestion[], Error>({
    queryKey: arKeys.suggestion(invoiceId ?? ''),
    enabled: invoiceId != null,
    queryFn: ({ signal }) =>
      get<AdvanceSuggestion[]>(`/billing/invoices/${invoiceId}/advance-suggestion`, signal),
  });
}

// --- mutations ----------------------------------------------------------------

/** Record a payment against an invoice (OPERATE). 422 if it exceeds outstanding. */
export function useRecordPayment(): UseMutationResult<Payment, ApiError, PaymentInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Payment, ApiError, PaymentInput>({
    mutationFn: (body) => post<Payment>('/billing/payments', body),
    onSuccess: () => invalidateAll(qc),
  });
}

/** Record a client advance/deposit (OPERATE). */
export function useRecordAdvance(): UseMutationResult<Advance, ApiError, AdvanceInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Advance, ApiError, AdvanceInput>({
    mutationFn: (body) => post<Advance>('/billing/advances', body),
    onSuccess: () => invalidateAll(qc),
  });
}

/** Apply an advance to an invoice (OPERATE). 422 on over-remaining / over-outstanding. */
export function useApplyAdvance(): UseMutationResult<Application, ApiError, ApplicationInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Application, ApiError, ApplicationInput>({
    mutationFn: (body) => post<Application>('/billing/advance-applications', body),
    onSuccess: () => invalidateAll(qc),
  });
}

/** Reverse an advance application by id (OPERATE) — frees remaining + re-opens outstanding. */
export function useUnapplyAdvance(): UseMutationResult<{ ok: boolean }, ApiError, number> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<{ ok: boolean }, ApiError, number>({
    mutationFn: (applicationId) => del<{ ok: boolean }>(`/billing/advance-applications/${applicationId}`),
    onSuccess: () => invalidateAll(qc),
  });
}
