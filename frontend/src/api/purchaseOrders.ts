import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Purchase Orders slice of the Sales
 * Orders module (module key `sales_orders`). Mirrors the backend
 * app/modules/sales_orders/po_routes.py. All paths are relative to `/api/v1`
 * (added by `useApi()`). MONEY is integer PAISE on the wire — form inputs are in
 * rupees and converted with {@link rupeesToPaise} before being sent.
 *
 * Ids are treated as strings (mirroring api/projects.ts + api/products.ts): they
 * only ever flow through `<select>` values, query strings, and equality checks —
 * never arithmetic. FastAPI coerces the numeric-string ids back to ints.
 */

// --- status unions ------------------------------------------------------------

/** A purchase order's lifecycle status (backend POStatus / ck_purchase_order_status). */
export type POStatus = 'DRAFT' | 'CONFIRMED' | 'IN_PROGRESS' | 'CLOSED' | 'CANCELLED';

/** All PO statuses, in display order (used by the register status filter). */
export const PO_STATUSES: readonly POStatus[] = [
  'DRAFT',
  'CONFIRMED',
  'IN_PROGRESS',
  'CLOSED',
  'CANCELLED',
];

/** A single PO line's status (backend LineStatus / ck_po_line_status). */
export type LineStatus = 'OPEN' | 'SHORT_CLOSED' | 'CLOSED';

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Sentence-case labels + badge tones for a PO's lifecycle status. */
export const PO_STATUS_LABEL: Record<POStatus, string> = {
  DRAFT: 'Draft',
  CONFIRMED: 'Confirmed',
  IN_PROGRESS: 'In progress',
  CLOSED: 'Closed',
  CANCELLED: 'Cancelled',
};

export const PO_STATUS_TONE: Record<POStatus, Tone> = {
  DRAFT: 'slate',
  CONFIRMED: 'blue',
  IN_PROGRESS: 'amber',
  CLOSED: 'green',
  CANCELLED: 'red',
};

export const LINE_STATUS_LABEL: Record<LineStatus, string> = {
  OPEN: 'Open',
  SHORT_CLOSED: 'Short-closed',
  CLOSED: 'Closed',
};

export const LINE_STATUS_TONE: Record<LineStatus, Tone> = {
  OPEN: 'blue',
  SHORT_CLOSED: 'amber',
  CLOSED: 'slate',
};

// --- money seam ---------------------------------------------------------------

/**
 * Parse an operator-typed rupee string into integer PAISE, or null when it is not
 * a valid amount. Accepts an optional ₹, thousands separators, surrounding spaces,
 * and up to two decimals. The backend takes money as integer paise, so this is the
 * single ₹→paise seam for the PO form (mirrors `rupees()` on the read path).
 */
export function rupeesToPaise(input: string): number | null {
  const cleaned = input.replace(/[₹,\s]/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  return Math.round(parseFloat(cleaned) * 100);
}

// --- DTOs ---------------------------------------------------------------------

/** A register row (backend `POSummaryOut`). */
export interface POSummary {
  id: string;
  po_number: string;
  client_id: string;
  client_name: string | null;
  project_id: string;
  project_code: string | null;
  /** ISO date string (backend `date`). */
  po_date: string;
  expected_procurement_date: string | null;
  status: POStatus;
  line_count: number;
  /** PAISE — sum of every line's sell value. */
  total_sell_paise: number;
  /** ISO datetime string. */
  created_at: string;
}

/** A stored soft-copy file reference (backend `SoftCopyOut`). */
export interface SoftCopyFile {
  id: string;
  filename: string;
}

/** One line on a PO detail (backend `POLineOut`). */
export interface POLine {
  id: string;
  product_id: string;
  product_name: string | null;
  brand: string | null;
  model_number: string | null;
  description: string;
  uom: string;
  /** Decimal serialised as a string. */
  ordered_qty: string;
  cost_price_paise: number;
  sell_price_paise: number;
  freight_paise: number;
  packaging_paise: number;
  handling_paise: number;
  other_paise: number;
  /** Decimal serialised as a string, e.g. "18.00". */
  tax_rate: string;
  line_status: LineStatus;
  short_closed_qty: string;
  short_close_reason: string | null;
}

/** Full PO detail (backend `PODetailOut` = summary + these). */
export interface PODetail extends POSummary {
  client_gstin: string | null;
  notes: string | null;
  soft_copy_file: SoftCopyFile | null;
  amendments_count: number;
  lines: POLine[];
}

/** One line on the create/amend request (backend `POLineIn`). Money is PAISE. */
export interface POLineInput {
  product_id: string;
  description?: string;
  uom?: string;
  /** Decimal string or number; must be > 0. */
  ordered_qty: string;
  cost_price_paise: number;
  sell_price_paise: number;
  freight_paise?: number;
  packaging_paise?: number;
  handling_paise?: number;
  other_paise?: number;
  /** 0..100. */
  tax_rate?: number;
}

/** Create body (backend `POCreateIn`). */
export interface POCreateInput {
  po_number: string;
  client_id: string;
  client_gstin_id?: string | null;
  project_id: string;
  /** YYYY-MM-DD. */
  po_date: string;
  expected_procurement_date?: string | null;
  notes?: string | null;
  soft_copy_file_id?: string | null;
  lines: POLineInput[];
}

/** Amend body (backend `POAmendIn`) — only supplied keys apply; `lines` fully replaces. */
export interface POAmendInput {
  po_number?: string;
  client_gstin_id?: string | null;
  project_id?: string;
  po_date?: string;
  expected_procurement_date?: string | null;
  notes?: string | null;
  soft_copy_file_id?: string | null;
  lines?: POLineInput[];
  /** A human note on what/why this amendment changed. */
  summary?: string;
}

/** One skipped PO in a bulk upload (an existing po_number). */
export interface BulkSkip {
  po_number: string;
  reason: string;
}

/** One row-level error in a bulk upload (unknown product / malformed row). */
export interface BulkError {
  row: number;
  reason: string;
}

/** The result of a bulk .xlsx upload (backend `BulkUploadOut`). */
export interface BulkUploadOut {
  created: string[];
  skipped: BulkSkip[];
  errors: BulkError[];
}

/** A choosable product for the line-item picker (lean projection of `/products`). */
export interface PickerProduct {
  id: string;
  code: string | null;
  name: string;
  brand: string | null;
  model_number: string | null;
  uom: string;
}

/** A client's GSTIN, for the optional GSTIN picker (subset of the projects `GstinOut`). */
export interface ClientGstin {
  id: string;
  gstin: string;
  legal_name: string | null;
  is_default: boolean;
}

export interface POFilters {
  client_id?: string;
  project_id?: string;
  status?: POStatus | '';
  /** Free-text search over po_number. */
  q?: string;
}

// --- query-string builder (pure, unit-testable) -------------------------------

/**
 * Build a `?client_id=&project_id=&status=&q=` query for the register, appending
 * only non-empty (trimmed) params.
 */
export function buildPurchaseOrdersQuery(filters: POFilters): string {
  const params = new URLSearchParams();
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.project_id?.trim()) params.set('project_id', filters.project_id.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const poKeys = {
  list: (filters: POFilters) => ['purchase-orders', 'list', filters] as const,
  // Normalise the id to a string so the detail QUERY (URL param = string) and every
  // mutation's setQueryData/invalidation (backend `po.id` = runtime number) land on
  // the SAME cache key — otherwise the open detail never reflects a short-close / void
  // / amend without a manual refetch.
  detail: (id: string | number) => ['purchase-orders', 'detail', String(id)] as const,
  products: ['purchase-orders', 'product-picker'] as const,
  clientGstins: (clientId: string) => ['purchase-orders', 'client-gstins', clientId] as const,
};

// --- queries ------------------------------------------------------------------

/** The PO register, filtered by client / project / status / free-text (VIEW). */
export function usePurchaseOrdersQuery(filters: POFilters): UseQueryResult<POSummary[], Error> {
  const { get } = useApi();
  return useQuery<POSummary[], Error>({
    queryKey: poKeys.list(filters),
    queryFn: ({ signal }) =>
      get<POSummary[]>(`/purchase-orders${buildPurchaseOrdersQuery(filters)}`, signal),
  });
}

/** A single PO with its lines (VIEW). */
export function usePurchaseOrderQuery(id: string | null): UseQueryResult<PODetail, Error> {
  const { get } = useApi();
  return useQuery<PODetail, Error>({
    queryKey: poKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<PODetail>(`/purchase-orders/${id}`, signal),
  });
}

/**
 * Active products for the line-item picker. Self-contained (GETs `/products?active=true`
 * directly) so this file does not depend on the parallel Products FE agent's api module.
 */
export function useProductPicker(): UseQueryResult<PickerProduct[], Error> {
  const { get } = useApi();
  return useQuery<PickerProduct[], Error>({
    queryKey: poKeys.products,
    queryFn: ({ signal }) => get<PickerProduct[]>('/products?active=true', signal),
  });
}

/**
 * A client's active GSTINs, for the optional GSTIN picker on the create form. Reads
 * the projects client-detail endpoint and projects out just the GSTIN list; disabled
 * until a client is chosen.
 */
export function useClientGstinsQuery(clientId: string | null): UseQueryResult<ClientGstin[], Error> {
  const { get } = useApi();
  return useQuery<ClientGstin[], Error>({
    queryKey: poKeys.clientGstins(clientId ?? ''),
    enabled: !!clientId,
    queryFn: async ({ signal }) => {
      const detail = await get<{ gstins: ClientGstin[] }>(
        `/projects/clients/${clientId}`,
        signal,
      );
      return detail.gstins ?? [];
    },
  });
}

// --- mutations ----------------------------------------------------------------

/** Create a PO (OPERATE). Invalidates the register on success. */
export function useCreatePurchaseOrder(): UseMutationResult<PODetail, ApiError, POCreateInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<PODetail, ApiError, POCreateInput>({
    mutationFn: (body) => post<PODetail>('/purchase-orders', body),
    onSuccess: (po) => {
      qc.setQueryData(poKeys.detail(po.id), po);
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
  });
}

/** Amend a PO header and/or lines (OPERATE). 422 if the PO is CLOSED/CANCELLED. */
export function useAmendPurchaseOrder(): UseMutationResult<
  PODetail,
  ApiError,
  { id: string; body: POAmendInput }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<PODetail, ApiError, { id: string; body: POAmendInput }>({
    mutationFn: ({ id, body }) => patch<PODetail>(`/purchase-orders/${id}`, body),
    onSuccess: (po) => {
      qc.setQueryData(poKeys.detail(po.id), po);
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
  });
}

/** Short-close a single line (with `line_id`) or the whole PO (MANAGE). */
export function useShortClosePO(): UseMutationResult<
  PODetail,
  ApiError,
  { id: string; line_id?: string; reason: string }
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<PODetail, ApiError, { id: string; line_id?: string; reason: string }>({
    mutationFn: ({ id, line_id, reason }) =>
      post<PODetail>(`/purchase-orders/${id}/short-close`, { line_id, reason }),
    onSuccess: (po) => {
      qc.setQueryData(poKeys.detail(po.id), po);
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
  });
}

/** Void (cancel) a PO — soft, nothing deleted (MANAGE). */
export function useVoidPO(): UseMutationResult<PODetail, ApiError, { id: string; reason: string }> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<PODetail, ApiError, { id: string; reason: string }>({
    mutationFn: ({ id, reason }) => post<PODetail>(`/purchase-orders/${id}/void`, { reason }),
    onSuccess: (po) => {
      qc.setQueryData(poKeys.detail(po.id), po);
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
  });
}

/** Arguments for a bulk .xlsx upload — one client + project tags the whole batch. */
export interface BulkUploadArgs {
  file: File;
  clientId: string;
  projectId: string;
}

/**
 * Bulk-create POs from an .xlsx (OPERATE). Sends a multipart form with `file`,
 * `client_id`, `project_id`. Returns the created / skipped / errored breakdown.
 * Invalidates the register on success.
 */
export function useBulkUploadPOs(): UseMutationResult<BulkUploadOut, ApiError, BulkUploadArgs> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<BulkUploadOut, ApiError, BulkUploadArgs>({
    mutationFn: ({ file, clientId, projectId }) => {
      const form = new FormData();
      form.append('file', file);
      form.append('client_id', clientId);
      form.append('project_id', projectId);
      return postForm<BulkUploadOut>('/purchase-orders/upload', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
  });
}
