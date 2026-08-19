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
import { useApi } from './client';

/**
 * Typed contracts + React Query hooks for the Expense / Invoice module.
 * Mirrors backend/app/modules/expense/routes.py. All paths are relative to
 * `/api/v1` (added by `useApi()`). Money is integer PAISE on the wire.
 */

// --- status unions ------------------------------------------------------------

/** A stored invoice's lifecycle status (backend Invoice.status CheckConstraint). */
export type InvoiceStatus =
  | 'UPLOADED'
  | 'EXTRACTED'
  | 'NEEDS_REVIEW'
  | 'NEEDS_OCR'
  | 'CONFIRMED'
  | 'REJECTED';

/**
 * The per-file outcome of an upload. Adds the upload-only `DUPLICATE` (a file
 * whose invoice collides an existing one) to the stored-status set; a duplicate
 * is never persisted, so it is not an `InvoiceStatus`.
 */
export type UploadResultStatus =
  | 'EXTRACTED'
  | 'NEEDS_REVIEW'
  | 'NEEDS_OCR'
  | 'REJECTED'
  | 'DUPLICATE';

/** A single extracted field's confidence status (backend InvoiceField.status). */
export type FieldStatus = 'OK' | 'LOW_CONFIDENCE' | 'MISSING' | 'CORRECTED';

// --- DTOs ---------------------------------------------------------------------

/**
 * A register row — EXACTLY the scalars the list endpoint returns (backend
 * `InvoiceOut`). The list is a lean projection: it does NOT carry review_reasons,
 * buyer_*, place_of_supply, the tax sub-totals, round-off, or amount_in_words —
 * those live only on the detail response. Keep this in lock-step with the backend
 * `InvoiceOut` model, not with the fuller `InvoiceDetail`.
 */
export interface InvoiceOut {
  id: number;
  status: InvoiceStatus;
  needs_ocr: boolean;
  supplier_name: string | null;
  supplier_gstin: string | null;
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  invoice_date: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  grand_total_paise: number | null;
  /** ISO datetime string (backend `created_at`). */
  created_at: string;
}

/** One extracted field envelope (backend FieldOut). */
export interface FieldOut {
  /** e.g. "header.supplier_gstin" | "totals.grand_total_paise" | "line.3.taxable_paise". */
  field_path: string;
  value_normalized: string | null;
  value_raw: string;
  /** 0..1 calibrated confidence (backend `FieldOut.confidence` is a non-null float). */
  confidence: number;
  status: FieldStatus;
}

/** One line item on an invoice (backend LineOut). */
export interface LineOut {
  line_no: number;
  description: string | null;
  hsn_sac: string | null;
  /** Decimal serialised as a string (Numeric(14,3)); null when absent. */
  quantity: string | null;
  unit: string | null;
  unit_rate_paise: number | null;
  taxable_paise: number | null;
  /** Decimal serialised as a string (Numeric(5,2)). */
  gst_rate: string | null;
  cgst_paise: number | null;
  sgst_paise: number | null;
  igst_paise: number | null;
  line_total_paise: number | null;
}

/**
 * Full invoice detail (backend `InvoiceDetailOut`, GET /expense/invoices/{id} and
 * the PATCH review response). A distinct, richer shape than the list `InvoiceOut`
 * — it adds review_reasons, the buyer/address block, place-of-supply, the full tax
 * sub-totals + round-off + amount-in-words, confirmation audit, and the per-field
 * envelopes + line items. Kept standalone (not `extends InvoiceOut`) so the list
 * projection can stay lean without leaking detail-only fields onto register rows.
 */
export interface InvoiceDetail {
  id: number;
  batch_id: number;
  status: InvoiceStatus;
  needs_ocr: boolean;
  review_reasons: string[];
  source_file_id: number | null;
  supplier_name: string | null;
  supplier_gstin: string | null;
  supplier_address: string | null;
  buyer_name: string | null;
  buyer_gstin: string | null;
  buyer_address: string | null;
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  invoice_date: string | null;
  place_of_supply: string | null;
  po_ref: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  total_cgst_paise: number | null;
  total_sgst_paise: number | null;
  total_igst_paise: number | null;
  /** PAISE, SIGNED (round-off can be negative). */
  round_off_paise: number | null;
  grand_total_paise: number | null;
  amount_in_words: string | null;
  confirmed_by: string | null;
  /** ISO datetime string; null until confirmed. */
  confirmed_at: string | null;
  fields: FieldOut[];
  lines: LineOut[];
}

/**
 * One file's outcome inside an upload batch (backend `FileOutcomeOut`). The
 * summary scalars (`supplier_name` / `invoice_number` / `grand_total_paise`) and
 * `duplicate_of` are FLAT: on a DUPLICATE they describe the EXISTING invoice, and
 * `duplicate_of` is that stored invoice's id (used to delete-and-re-upload).
 */
export interface UploadResult {
  file_id: number;
  filename: string;
  status: UploadResultStatus;
  invoice_id?: number | null;
  /** The EXISTING invoice id on a DUPLICATE; null/absent otherwise. */
  duplicate_of?: number | null;
  supplier_name?: string | null;
  invoice_number?: string | null;
  /** PAISE. */
  grand_total_paise?: number | null;
  review_reasons?: string[];
  message?: string | null;
}

/** The response of a bulk upload (backend POST /expense/invoices → UploadOut). */
export interface UploadBatchOut {
  batch_id: number;
  invoice_count: number;
  outcomes: UploadResult[];
}

/**
 * One field correction submitted from the review panel (backend `CorrectionItem`).
 * The wire field is `value` (NOT `new_value` — that name is only the backend's
 * audit column). For a money (`*_paise`) field this MUST be an integer-paise
 * string, because the backend coerces money corrections with `int(text)`; the
 * review panel converts the operator's rupee input to paise before building this.
 */
export interface Correction {
  field_path: string;
  value: string;
}

/** Register page size for the "Load more" pager (mirrors the backend list default). */
export const EXPENSE_PAGE_SIZE = 100;

export interface InvoiceFilters {
  /** Free-text search over supplier / GSTIN / invoice number. */
  q?: string;
  status?: InvoiceStatus | '';
  /** Inclusive lower bound on invoice_date (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound on invoice_date (YYYY-MM-DD). */
  date_to?: string;
}

// --- query-string builders (pure, unit-testable) ------------------------------

/**
 * Append the shared register filters (q / status / date_from / date_to) to a
 * params bag, each only when non-empty (trimmed). Shared by the list query and
 * the CSV export so their filter semantics stay identical.
 */
function appendInvoiceFilters(params: URLSearchParams, filters: InvoiceFilters): void {
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
}

/**
 * Build the `?q=&status=&date_from=&date_to=&limit=&offset=` query for the
 * register list. Filter params are appended only when non-empty (trimmed);
 * limit/offset are always sent so the register never silently rides the backend's
 * default 100-row cap.
 */
export function buildInvoiceListQuery(filters: InvoiceFilters, offset: number): string {
  const params = new URLSearchParams();
  appendInvoiceFilters(params, filters);
  params.set('limit', String(EXPENSE_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

/** Build the query for the CSV export — identical filters, no paging. */
export function buildInvoiceCsvQuery(filters: InvoiceFilters): string {
  const params = new URLSearchParams();
  appendInvoiceFilters(params, filters);
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const expenseKeys = {
  invoices: (filters: InvoiceFilters) => ['expense', 'invoices', filters] as const,
  invoice: (id: number) => ['expense', 'invoice', id] as const,
};

// --- queries ------------------------------------------------------------------

/**
 * The invoice register, filtered by q / status / date range, with offset-based
 * "Load more" paging. Each page fetches up to `EXPENSE_PAGE_SIZE` rows; there is
 * another page only when the last one came back exactly full (a short OR empty
 * page means the end). The offset for the next page is the running total already
 * loaded. Mirrors the challan register so paging semantics stay identical.
 */
export function useInvoicesInfiniteQuery(
  filters: InvoiceFilters,
): UseInfiniteQueryResult<InfiniteData<InvoiceOut[], number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    InvoiceOut[],
    Error,
    InfiniteData<InvoiceOut[], number>,
    readonly unknown[],
    number
  >({
    queryKey: expenseKeys.invoices(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<InvoiceOut[]>(`/expense/invoices${buildInvoiceListQuery(filters, pageParam)}`, signal),
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length > 0 && lastPage.length === EXPENSE_PAGE_SIZE
        ? allPages.reduce((total, page) => total + page.length, 0)
        : undefined,
  });
}

/** A single invoice with its per-field envelopes + line items. */
export function useInvoice(
  invoiceId: number | null,
): UseQueryResult<InvoiceDetail, Error> {
  const { get } = useApi();
  return useQuery<InvoiceDetail, Error>({
    queryKey: expenseKeys.invoice(invoiceId ?? -1),
    enabled: invoiceId != null,
    queryFn: ({ signal }) =>
      get<InvoiceDetail>(`/expense/invoices/${invoiceId}`, signal),
  });
}

// --- mutations ----------------------------------------------------------------

/**
 * Bulk-upload N PDFs (one invoice each) as a single multipart batch under the
 * `files` field. Returns the per-file outcomes (incl. any DUPLICATE carrying the
 * existing invoice's summary). The delete-and-re-upload flow reuses this hook
 * with a single-element array. Invalidates the register on success.
 */
export function useUploadInvoices(): UseMutationResult<UploadBatchOut, Error, File[]> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<UploadBatchOut, Error, File[]>({
    mutationFn: (files) => {
      const form = new FormData();
      for (const f of files) form.append('files', f);
      return postForm<UploadBatchOut>('/expense/invoices', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['expense', 'invoices'] });
    },
  });
}

/**
 * Submit per-field corrections for an invoice, optionally confirming it. On
 * `confirm: true` the backend freezes the canonical scalars (state → CONFIRMED).
 * Seeds the detail cache and refreshes the register on success.
 */
export function useSubmitReview(): UseMutationResult<
  InvoiceDetail,
  Error,
  { invoiceId: number; corrections: Correction[]; confirm?: boolean }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    InvoiceDetail,
    Error,
    { invoiceId: number; corrections: Correction[]; confirm?: boolean }
  >({
    mutationFn: ({ invoiceId, corrections, confirm }) =>
      patch<InvoiceDetail>(`/expense/invoices/${invoiceId}/reviews`, { corrections, confirm }),
    onSuccess: (invoice) => {
      void qc.invalidateQueries({ queryKey: expenseKeys.invoice(invoice.id) });
      void qc.invalidateQueries({ queryKey: ['expense', 'invoices'] });
    },
  });
}

/**
 * Delete an invoice (204). Used by the delete-and-re-upload flow when an upload
 * collides an existing invoice. Invalidates the register on success.
 */
export function useDeleteInvoice(): UseMutationResult<void, Error, number> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<void, Error, number>({
    mutationFn: (invoiceId) => del<void>(`/expense/invoices/${invoiceId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['expense', 'invoices'] });
    },
  });
}
