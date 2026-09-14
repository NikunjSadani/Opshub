import { useCallback } from 'react';
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

/** How a PO-header agency fee is charged (backend AgencyFeeType). */
export type AgencyFeeType = 'NONE' | 'PERCENT' | 'FIXED';

export const AGENCY_FEE_LABEL: Record<AgencyFeeType, string> = {
  NONE: 'None',
  PERCENT: '% of order value',
  FIXED: 'Fixed ₹',
};

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
  /** Now OPTIONAL on the PO — may be null and added later via an amend. */
  po_number: string | null;
  client_id: string;
  client_name: string | null;
  project_id: string;
  project_code: string | null;
  /** ISO date string (backend `date`). */
  po_date: string;
  expected_procurement_date: string | null;
  status: POStatus;
  line_count: number;
  /** PAISE — sum of every line's ACTUAL sell value. ADMIN-ONLY: the API returns `null`
   * for non-admins, so this is nullable and must be rendered with `rupeesOrDash`. */
  total_sell_paise: number | null;
  /** PAISE — sum of every line's CLIENT-quoted GOODS value. Visible to everyone. */
  total_client_sell_paise: number;
  /** PAISE — sum of every live line's CLIENT-quoted freight. Revenue billed to the client;
   * visible to everyone. */
  total_client_freight_paise: number;
  /** PAISE — packaging + handling + other flat per-line client charges (revenue). Visible. */
  total_client_extras_paise: number;
  /** Header-level agency fee, charged on the entire client billing. Visible to everyone. */
  agency_fee_type: AgencyFeeType;
  /** Set when `agency_fee_type === 'PERCENT'` (e.g. 2.5 for 2.5%). */
  agency_fee_percent: number | null;
  /** Set (PAISE) when `agency_fee_type === 'FIXED'`. */
  agency_fee_amount_paise: number | null;
  /** PAISE — the resolved agency fee (percent-of-entire-client-billing or the fixed amount).
   * Agency fee is REVENUE we charge the client, not a cost — visible to everyone. */
  agency_fee_computed_paise: number;
  /** PAISE — entire client billing (goods + freight + packaging/handling/other) + agency fee
   * = full client-facing revenue. */
  total_with_agency_paise: number;
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
  /** Our CP (billed) — always visible. */
  cost_price_paise: number;
  /** Original CP (optional) — visible. */
  original_cost_price_paise: number | null;
  /** Client-quoted sell — the primary shown sell; visible. */
  client_sell_price_paise: number;
  /** Vendor sell (optional) — visible. */
  vendor_sell_price_paise: number | null;
  /** Actual sell — ADMIN-ONLY: the API returns null for non-admins. */
  sell_price_paise: number | null;
  /** Client freight (optional) — visible. */
  client_freight_paise: number | null;
  /** Vendor freight (optional) — visible. */
  vendor_freight_paise: number | null;
  /** Actual freight — ADMIN-ONLY: the API returns null for non-admins. */
  freight_paise: number | null;
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
  // Agency fee (type/percent/amount + computed + with-agency total) is inherited from
  // POSummary — the backend returns it on both the summary and the detail shapes.
  lines: POLine[];
}

/** One line on the create/amend request (backend `POLineIn`). Money is PAISE. */
export interface POLineInput {
  product_id: string;
  description?: string;
  uom?: string;
  /** Decimal string or number; must be > 0. */
  ordered_qty: string;
  /** Our CP (billed) — required. */
  cost_price_paise: number;
  /** Original CP — optional. */
  original_cost_price_paise?: number;
  /** Client-quoted sell — required. */
  client_sell_price_paise: number;
  /** Vendor sell — optional. */
  vendor_sell_price_paise?: number;
  /** Actual sell — ADMIN-ONLY; omit unless the operator is an admin. */
  sell_price_paise?: number;
  /** Client freight — optional. */
  client_freight_paise?: number;
  /** Vendor freight — optional. */
  vendor_freight_paise?: number;
  /** Actual freight — ADMIN-ONLY; omit unless the operator is an admin. */
  freight_paise?: number;
  packaging_paise?: number;
  handling_paise?: number;
  other_paise?: number;
  /** 0..100. */
  tax_rate?: number;
}

/** The agency-fee slice shared by create + amend headers. */
export interface AgencyFeeInput {
  agency_fee_type?: AgencyFeeType;
  /** Sent when `agency_fee_type === 'PERCENT'`. */
  agency_fee_percent?: number;
  /** Sent (PAISE) when `agency_fee_type === 'FIXED'`. */
  agency_fee_amount_paise?: number;
}

/** Create body (backend `POCreateIn`). */
export interface POCreateInput extends AgencyFeeInput {
  /** Optional now — a PO can be created without a number and get one later. */
  po_number?: string | null;
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
export interface POAmendInput extends AgencyFeeInput {
  /** Add or change the PO number (it may have been created without one). */
  po_number?: string | null;
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
  /** Page size (backend clamps 1..200, defaults 50). Set it for a bounded scoped view
   * — e.g. a project's drill-through wants the whole set, not the register's first page. */
  limit?: number;
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
  if (filters.limit != null) params.set('limit', String(filters.limit));
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
 * SERVER-searched active products for the line-item picker. There can be hundreds of
 * products, so the combobox drives this with its debounced query text: it GETs
 * `/products?q=<query>&active=true&limit=200`. An empty query loads the first page.
 * `keepPreviousData` keeps the last results on screen while the next page loads so the
 * list doesn't flicker as the operator types.
 *
 * Optional `projectId` scopes the picker to a project's TAGGED (curated) products: when
 * it's a non-empty string it's appended as `project_id`, and the backend returns only
 * that project's tagged active products while the query is EMPTY, falling back to the
 * full catalogue as soon as the operator types. It's part of the query key so a scoped
 * result never cross-contaminates the unscoped (any-catalogue) cache. Omit it for a
 * plain full-catalogue search — existing callers keep working unchanged.
 */
export function useProductSearch(
  query: string,
  projectId?: string,
): UseQueryResult<PickerProduct[], Error> {
  const { get } = useApi();
  const q = query.trim();
  const pid = projectId?.trim() ?? '';
  return useQuery<PickerProduct[], Error>({
    queryKey: [...poKeys.products, 'search', q, pid] as const,
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ active: 'true', limit: '200' });
      if (q) params.set('q', q);
      if (pid) params.set('project_id', pid);
      return get<PickerProduct[]>(`/products?${params.toString()}`, signal);
    },
    placeholderData: (prev) => prev,
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

/** Confirm a DRAFT PO — moves it to CONFIRMED (OPERATE). 422 if the PO isn't DRAFT. */
export function useConfirmPurchaseOrder(): UseMutationResult<
  PODetail,
  ApiError,
  { id: string }
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<PODetail, ApiError, { id: string }>({
    mutationFn: ({ id }) => post<PODetail>(`/purchase-orders/${id}/confirm`, {}),
    onSuccess: (po) => {
      qc.setQueryData(poKeys.detail(String(po.id)), po);
      void qc.invalidateQueries({ queryKey: ['purchase-orders', 'list'] });
    },
    // A 409/422 usually means the PO was already confirmed elsewhere (a concurrent operator);
    // refetch so the badge reconciles to CONFIRMED and the now-invalid Confirm button clears,
    // instead of leaving a stale DRAFT view that keeps failing on re-click.
    onError: (_err, { id }) => {
      void qc.invalidateQueries({ queryKey: poKeys.detail(id) });
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

/** The auth-gated endpoint that serves the downloadable bulk-upload .xlsx template (OPERATE). */
export const PO_BULK_TEMPLATE_PATH = '/purchase-orders/bulk-template.xlsx';
/** The filename the template downloads as (fallback if the server omits Content-Disposition). */
export const PO_BULK_TEMPLATE_FILENAME = 'po-bulk-template.xlsx';

/**
 * Download the bulk-upload .xlsx template through the authed blob helper. The endpoint is
 * auth-gated (OPERATE), so a bare `<a href>` would 401 — this reuses `downloadUrl`, which
 * attaches the bearer token, streams the blob, and saves it with the server's filename.
 * Returns a callback the button can await.
 */
export function useDownloadBulkTemplate(): () => Promise<void> {
  const { downloadUrl } = useApi();
  return useCallback(async () => {
    await downloadUrl(PO_BULK_TEMPLATE_PATH, PO_BULK_TEMPLATE_FILENAME);
  }, [downloadUrl]);
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
