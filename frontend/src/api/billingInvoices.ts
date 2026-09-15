import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the client-invoice capture + match slice
 * of the Billing & AR module (module key `billing`). Mirrors the backend
 * app/modules/billing/invoice_routes.py. All paths are relative to `/api/v1`
 * (added by `useApi()`). MONEY is integer PAISE on the wire.
 *
 * Ids are treated as strings (mirroring api/projects.ts + api/purchaseOrders.ts):
 * they only ever flow through `<select>` values, query strings, route params, and
 * equality checks — never arithmetic. FastAPI coerces the numeric-string ids to ints.
 */

// --- status unions ------------------------------------------------------------

/** A stored sales-invoice's lifecycle status (backend SalesInvoiceStatus). */
export type SalesInvoiceStatus =
  | 'UPLOADED'
  | 'EXTRACTED'
  | 'NEEDS_REVIEW'
  | 'NEEDS_OCR'
  | 'NEEDS_MATCH'
  | 'MATCHED'
  | 'CONFIRMED'
  | 'REJECTED'
  | 'CANCELLED';

/**
 * The per-file outcome of an upload. Adds the upload-only `DUPLICATE` (a file whose
 * identity collides an existing invoice) to the in-review status set; a duplicate is
 * never persisted, so it is not a stored `SalesInvoiceStatus`.
 */
export type UploadOutcomeStatus =
  | 'EXTRACTED'
  | 'NEEDS_REVIEW'
  | 'NEEDS_OCR'
  | 'NEEDS_MATCH'
  | 'MATCHED'
  | 'REJECTED'
  | 'DUPLICATE';

/** A single extracted field's confidence status (backend FieldStatus). */
export type FieldStatus = 'OK' | 'LOW_CONFIDENCE' | 'MISSING' | 'CORRECTED';

/** A single invoice line's PO-match status (backend LineMatchStatus). */
export type LineMatchStatus = 'UNMATCHED' | 'MATCHED' | 'MANUAL';

// --- DTOs ---------------------------------------------------------------------

/**
 * A register row — EXACTLY the scalars the list endpoint returns (backend
 * `InvoiceOut`). A lean projection: it does NOT carry review_reasons, the tax
 * sub-totals, confirmation audit, fields, or lines — those live only on the detail
 * response. Keep in lock-step with the backend list `InvoiceOut`, not `InvoiceDetail`.
 */
export interface BillingInvoiceRow {
  id: string;
  status: SalesInvoiceStatus;
  needs_ocr: boolean;
  client_id: string;
  po_id: string | null;
  /**
   * The direct project attribution for a PO-less invoice (backend serialises the
   * project's integer id, or null). Unlike the string ids above, this stays a raw
   * number to mirror the backend `int | None` exactly; the UI compares it against a
   * project's string id via `String(project_id)`. When a PO is linked the invoice's
   * project comes from the PO — this direct attribution is for the standalone case.
   */
  project_id: number | null;
  supplier_gstin: string | null;
  buyer_gstin: string | null;
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  invoice_date: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  grand_total_paise: number | null;
  /** ISO datetime string (backend `created_at`). */
  created_at: string;
}

/** One extracted field envelope (backend `FieldOut`). */
export interface BillingFieldOut {
  /** e.g. "header.buyer_gstin" | "totals.grand_total_paise". */
  field_path: string;
  /** Backend key is `value_norm` (NOT `value_normalized`). Null when missing. */
  value_norm: string | null;
  value_raw: string | null;
  /** 0..1 calibrated confidence. */
  confidence: number;
  source_engine: string | null;
  status: FieldStatus;
}

/** One line item on an invoice, with its PO-match state (backend `LineOut`). */
export interface BillingLineOut {
  id: string;
  line_no: number;
  /** The matched PO line item id, or null when UNMATCHED. */
  po_line_item_id: string | null;
  match_status: LineMatchStatus;
  description: string | null;
  hsn_sac: string | null;
  /** Decimal serialised as a string; null when absent. */
  quantity: string | null;
  unit: string | null;
  unit_rate_paise: number | null;
  taxable_paise: number | null;
  /** Decimal serialised as a string, e.g. "18.00". */
  gst_rate: string | null;
  cgst_paise: number | null;
  sgst_paise: number | null;
  igst_paise: number | null;
  line_total_paise: number | null;
}

/**
 * Full invoice detail (backend `InvoiceDetailOut`, GET /billing/invoices/{id} and
 * the PATCH review / match responses). A richer shape than the list row — it adds
 * review_reasons, the tax sub-totals + round-off, confirmation audit, and the
 * per-field envelopes + line items with their match state.
 */
export interface BillingInvoiceDetail {
  id: string;
  batch_id: string | null;
  status: SalesInvoiceStatus;
  needs_ocr: boolean;
  review_reasons: string[];
  client_id: string;
  po_id: string | null;
  /**
   * Direct project attribution for a PO-less invoice (backend `int | None`). Kept as a
   * raw number (see {@link BillingInvoiceRow.project_id}); null when unattributed. When a
   * PO is linked the project comes from the PO and this control isn't shown.
   */
  project_id: number | null;
  source_file_id: string | null;
  supplier_gstin: string | null;
  buyer_gstin: string | null;
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  invoice_date: string | null;
  due_date: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  total_cgst_paise: number | null;
  total_sgst_paise: number | null;
  total_igst_paise: number | null;
  /** PAISE, SIGNED (round-off can be negative). */
  round_off_paise: number | null;
  grand_total_paise: number | null;
  confirmed_by: string | null;
  /** ISO datetime string; null until confirmed. */
  confirmed_at: string | null;
  fields: BillingFieldOut[];
  lines: BillingLineOut[];
}

/**
 * One file's outcome inside an upload batch (backend `FileOutcomeOut`). The summary
 * scalars (`buyer_gstin` / `invoice_number` / `grand_total_paise`) and `duplicate_of`
 * are FLAT: on a DUPLICATE they describe the EXISTING invoice, and `duplicate_of` is
 * that stored invoice's id (used to delete-and-re-upload).
 */
export interface BillingUploadOutcome {
  file_id: string;
  filename: string;
  status: UploadOutcomeStatus;
  invoice_id: string | null;
  /** The EXISTING invoice id on a DUPLICATE; null otherwise. */
  duplicate_of: string | null;
  buyer_gstin: string | null;
  invoice_number: string | null;
  /** PAISE. */
  grand_total_paise: number | null;
  review_reasons: string[];
  message: string | null;
}

/** The response of a bulk upload (backend POST /billing/invoices → UploadOut). */
export interface BillingUploadBatch {
  batch_id: string;
  invoice_count: number;
  outcomes: BillingUploadOutcome[];
}

/**
 * One field correction submitted from the review panel (backend `CorrectionItem`).
 * The wire field is `value`. For a money (`*_paise`) field this MUST be an
 * integer-paise string (the backend coerces money corrections to int); the review
 * screen converts the operator's rupee input to paise before building this.
 */
export interface BillingCorrection {
  field_path: string;
  value: string;
}

/** Register page size for the "Load more" pager (mirrors the backend list default). */
export const BILLING_INVOICE_PAGE_SIZE = 100;

export interface BillingInvoiceFilters {
  /** Free-text search over buyer / GSTIN / invoice number. */
  q?: string;
  status?: SalesInvoiceStatus | '';
  /** Restrict to one client (id as a string for the query). */
  client_id?: string;
  /** Restrict to one purchase order (id as a string). */
  po_id?: string;
  /** Inclusive lower bound on invoice_date (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound on invoice_date (YYYY-MM-DD). */
  date_to?: string;
}

// --- query-string builder (pure, unit-testable) -------------------------------

/**
 * Build the `?q=&status=&client_id=&po_id=&date_from=&date_to=&limit=&offset=` query
 * for the register list. Filter params are appended only when non-empty (trimmed);
 * limit/offset are always sent so the register never silently rides the backend's
 * default 100-row cap.
 */
export function buildBillingInvoiceListQuery(
  filters: BillingInvoiceFilters,
  offset: number,
): string {
  const params = new URLSearchParams();
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.po_id?.trim()) params.set('po_id', filters.po_id.trim());
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
  params.set('limit', String(BILLING_INVOICE_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

// --- query keys ---------------------------------------------------------------

export const billingInvoiceKeys = {
  all: ['billing', 'invoices'] as const,
  list: (filters: BillingInvoiceFilters) => ['billing', 'invoices', filters] as const,
  // Normalise the id to a string so the detail query (route param = string) and every
  // mutation's setQueryData / invalidation land on the SAME cache key regardless of
  // whether the backend serialised the id as a number.
  detail: (id: string | number) => ['billing', 'invoice', String(id)] as const,
};

/**
 * A CONFIRMED client invoice creates the receivable (AR) and increments the PO line
 * invoiced-qty; a cancel/delete reverses it. The invoice's own register/detail are handled
 * by each mutation, but the AR tracker and the PO detail/list are cross-module reads that
 * would otherwise show stale numbers on an already-mounted view (refetchOnWindowFocus is off
 * app-wide). Broad-but-cheap prefix invalidations; read-refresh only.
 */
function invalidateBillingCrossModule(qc: ReturnType<typeof useQueryClient>): void {
  void qc.invalidateQueries({ queryKey: ['billing', 'ar'] });    // AR register + details
  void qc.invalidateQueries({ queryKey: ['purchase-orders'] });  // PO detail invoiced-qty + list
}

// --- queries ------------------------------------------------------------------

/**
 * The invoice register, filtered by q / status / client / PO / date range, with
 * offset-based "Load more" paging. Each page fetches up to `BILLING_INVOICE_PAGE_SIZE`
 * rows; there is another page only when the last one came back exactly full (a short
 * OR empty page means the end). Mirrors the expense register's paging semantics.
 */
export function useBillingInvoicesQuery(
  filters: BillingInvoiceFilters,
): UseInfiniteQueryResult<InfiniteData<BillingInvoiceRow[], number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    BillingInvoiceRow[],
    Error,
    InfiniteData<BillingInvoiceRow[], number>,
    readonly unknown[],
    number
  >({
    queryKey: billingInvoiceKeys.list(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<BillingInvoiceRow[]>(
        `/billing/invoices${buildBillingInvoiceListQuery(filters, pageParam)}`,
        signal,
      ),
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length > 0 && lastPage.length === BILLING_INVOICE_PAGE_SIZE
        ? allPages.reduce((total, page) => total + page.length, 0)
        : undefined,
  });
}

/** A single invoice with its per-field envelopes + line items (and PO-match state). */
export function useBillingInvoiceQuery(
  invoiceId: string | null,
): UseQueryResult<BillingInvoiceDetail, Error> {
  const { get } = useApi();
  return useQuery<BillingInvoiceDetail, Error>({
    queryKey: billingInvoiceKeys.detail(invoiceId ?? ''),
    enabled: invoiceId != null && invoiceId !== '',
    queryFn: ({ signal }) =>
      get<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}`, signal),
  });
}

// --- mutations ----------------------------------------------------------------

/**
 * Arguments for a bulk upload. `clientId` is REQUIRED (the whole batch is tagged to
 * one client); `poId` is optional but, when given, must belong to that client (the
 * backend validates before storing any bytes). Ids are strings because they originate
 * from `<select>` values and ride the multipart form as text.
 */
export interface BillingUploadArgs {
  files: File[];
  clientId: string;
  poId?: string;
  /**
   * Optional direct project attribution for a PO-less batch. Sent only when NO PO is
   * chosen (a PO already carries the project); must be an ACTIVE project of the same
   * client (backend validates before storing any bytes). A string because it rides the
   * multipart form as text; omit/blank to leave the invoice unattributed.
   */
  projectId?: string;
}

/**
 * Bulk-upload N PDFs (one invoice each) as a single multipart batch under the `files`
 * field, tagged with a client (+ optional PO). Returns the per-file outcomes (incl.
 * any DUPLICATE carrying the existing invoice's summary). The delete-and-re-upload
 * flow reuses this hook with a single-element array. Invalidates the register on success.
 */
export function useUploadBillingInvoices(): UseMutationResult<
  BillingUploadBatch,
  ApiError,
  BillingUploadArgs
> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingUploadBatch, ApiError, BillingUploadArgs>({
    mutationFn: async ({ files, clientId, poId, projectId }) => {
      const form = new FormData();
      for (const f of files) form.append('files', f);
      form.append('client_id', clientId);
      if (poId) form.append('po_id', poId);
      // A PO already carries the project; only a PO-less batch sends a direct attribution.
      if (!poId && projectId) form.append('project_id', projectId);
      try {
        return await postForm<BillingUploadBatch>('/billing/invoices', form);
      } catch (err) {
        // When the WHOLE batch is duplicates the backend returns 409 — but the body still
        // carries the per-file outcomes (each DUPLICATE, with the existing invoice's id).
        // Surface it as a normal result so the screen shows the friendly "matched an existing
        // invoice — resolve below" list (with a link to the original) rather than a bare
        // "Upload failed: 409". Any other 409 (or a 409 without a NON-EMPTY outcomes list)
        // still throws — requiring ≥1 outcome guards `confirmReplace`'s `outcomes[0]` against a
        // hypothetical empty-body 409.
        if (err instanceof ApiError && err.status === 409 && err.body != null && typeof err.body === 'object') {
          const body = err.body as { outcomes?: unknown };
          if (Array.isArray(body.outcomes) && body.outcomes.length > 0) {
            return body as BillingUploadBatch;
          }
        }
        throw err;
      }
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * Submit per-field corrections for an invoice, optionally confirming it (OPERATE).
 * On `confirm: true` the backend freezes the canonical scalars (state → CONFIRMED),
 * and 409s unless every line is MATCHED/MANUAL and every required field is present.
 * Seeds the detail cache and refreshes the register on success.
 */
export function useSubmitReview(): UseMutationResult<
  BillingInvoiceDetail,
  ApiError,
  { invoiceId: string; corrections?: BillingCorrection[]; confirm?: boolean }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    BillingInvoiceDetail,
    ApiError,
    { invoiceId: string; corrections?: BillingCorrection[]; confirm?: boolean }
  >({
    mutationFn: ({ invoiceId, corrections = [], confirm }) =>
      patch<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/review`, {
        corrections,
        confirm: confirm ?? false,
      }),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
      // Only a CONFIRM writes AR + PO invoiced-qty — a corrections-only save does not.
      if (invoice.status === 'CONFIRMED') invalidateBillingCrossModule(qc);
    },
  });
}

/**
 * Re-run the auto-matcher over the invoice's still-unmatched lines (OPERATE). Seeds
 * the detail cache (the response carries the updated line match state) and refreshes
 * the register (the derived status may change).
 */
export function useRematch(): UseMutationResult<BillingInvoiceDetail, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingInvoiceDetail, ApiError, string>({
    mutationFn: (invoiceId) =>
      post<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/match`),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * Manually map one invoice line to a PO line item (OPERATE, status → MANUAL). Seeds
 * the detail cache with the response's fresh line state.
 */
export function useManualMatch(): UseMutationResult<
  BillingInvoiceDetail,
  ApiError,
  { invoiceId: string; lineId: string; poLineItemId: string }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    BillingInvoiceDetail,
    ApiError,
    { invoiceId: string; lineId: string; poLineItemId: string }
  >({
    mutationFn: ({ invoiceId, lineId, poLineItemId }) =>
      patch<BillingInvoiceDetail>(
        `/billing/invoices/${invoiceId}/lines/${lineId}/match`,
        { po_line_item_id: Number(poLineItemId) },
      ),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * A manual line-item write from the review screen's line editor. All fields are
 * OPTIONAL on the wire except `description`, which the backend requires (non-empty) on
 * an ADD. Money is INTEGER PAISE (`*_paise`), `quantity`/`gst_rate` are decimal STRINGS
 * (e.g. "10.5" / "18"). The UI edits money in rupees and converts via `parseRupeesToPaise`
 * before building this; it OMITS every key the operator left empty (a partial PATCH only
 * touches the keys sent). Mirrors the backend LineWrite; these edits are pre-confirm and
 * never touch the AR / PO rollup (only a CONFIRM does), so the hooks below do NOT run the
 * cross-module invalidation.
 */
export interface LineWrite {
  description?: string;
  hsn_sac?: string;
  quantity?: string;
  unit?: string;
  unit_rate_paise?: number;
  taxable_paise?: number;
  gst_rate?: string;
  cgst_paise?: number;
  sgst_paise?: number;
  igst_paise?: number;
  line_total_paise?: number;
}

/**
 * Add a manual line item to a still-editable invoice (OPERATE, POST
 * /billing/invoices/{id}/lines). Reachable even on a lineless (EXTRACTED) invoice — this
 * is how it gets its first line. `description` is required + non-empty (backend 400s a
 * blank add); 400s out-of-range money/qty/gst; 409s a non-editable invoice. Seeds the
 * response detail into the cache and refreshes the register (the derived status may change).
 */
export function useAddInvoiceLine(): UseMutationResult<
  BillingInvoiceDetail,
  ApiError,
  { invoiceId: string; body: LineWrite }
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingInvoiceDetail, ApiError, { invoiceId: string; body: LineWrite }>({
    mutationFn: ({ invoiceId, body }) =>
      post<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/lines`, body),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * Edit one manual line item (OPERATE, PATCH /billing/invoices/{id}/lines/{lineId}). The
 * body is a partial LineWrite — only the keys the operator set are sent. 400s out-of-range
 * money/qty/gst; 404s an unknown line; 409s a non-editable invoice. Seeds the response
 * detail into the cache and refreshes the register.
 */
export function useUpdateInvoiceLine(): UseMutationResult<
  BillingInvoiceDetail,
  ApiError,
  { invoiceId: string; lineId: string; body: LineWrite }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    BillingInvoiceDetail,
    ApiError,
    { invoiceId: string; lineId: string; body: LineWrite }
  >({
    mutationFn: ({ invoiceId, lineId, body }) =>
      patch<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/lines/${lineId}`, body),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * Delete one manual line item (OPERATE, DELETE /billing/invoices/{id}/lines/{lineId} →
 * the updated BillingInvoiceDetail). 404s an unknown line; 409s a non-editable invoice.
 * Seeds the response detail into the cache and refreshes the register.
 */
export function useDeleteInvoiceLine(): UseMutationResult<
  BillingInvoiceDetail,
  ApiError,
  { invoiceId: string; lineId: string }
> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingInvoiceDetail, ApiError, { invoiceId: string; lineId: string }>({
    mutationFn: ({ invoiceId, lineId }) =>
      del<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/lines/${lineId}`),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/**
 * Assign / change / clear the direct project attribution on a PO-less, still-editable
 * invoice (OPERATE, PATCH /billing/invoices/{id}/project). The argument is the project's
 * numeric id, or null to clear (fall back to unattributed); the backend 400s unless it is
 * an ACTIVE project of the invoice's client, and 409s once the invoice is CONFIRMED. Seeds
 * the response detail into the cache (so the control reflects the new value) and refreshes
 * the register (whose row carries project_id).
 */
export function useSetInvoiceProject(
  invoiceId: string,
): UseMutationResult<BillingInvoiceDetail, ApiError, number | null> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingInvoiceDetail, ApiError, number | null>({
    mutationFn: (projectId) =>
      patch<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/project`, {
        project_id: projectId,
      }),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.detail(invoice.id) });
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
    },
  });
}

/** Soft-cancel an in-review invoice (MANAGE). 409 on a confirmed/already-cancelled one. */
export function useCancelInvoice(): UseMutationResult<BillingInvoiceDetail, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<BillingInvoiceDetail, ApiError, string>({
    mutationFn: (invoiceId) =>
      post<BillingInvoiceDetail>(`/billing/invoices/${invoiceId}/cancel`),
    onSuccess: (invoice) => {
      qc.setQueryData(billingInvoiceKeys.detail(invoice.id), invoice);
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
      invalidateBillingCrossModule(qc);  // reverses any AR/PO effect if this invoice had one
    },
  });
}

/**
 * Delete an invoice + its source blob (MANAGE, 200 with `{id, deleted}`). Used by the
 * delete-and-re-upload flow and by the detail screen's Delete action. Invalidates the
 * register on success.
 */
export function useDeleteInvoice(): UseMutationResult<
  { id: number; deleted: boolean },
  ApiError,
  string
> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<{ id: number; deleted: boolean }, ApiError, string>({
    mutationFn: (invoiceId) => del<{ id: number; deleted: boolean }>(`/billing/invoices/${invoiceId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: billingInvoiceKeys.all });
      invalidateBillingCrossModule(qc);  // reverses any AR/PO effect if this invoice had one
    },
  });
}
