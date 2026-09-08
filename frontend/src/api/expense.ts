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

/**
 * The document kind of a stored row. An INVOICE is a spend; a CREDIT_NOTE is a
 * vendor REDUCTION of a prior invoice — its `grand_total_paise` still arrives as a
 * POSITIVE magnitude on the wire, so any spend-facing surface must render it as a
 * negative/parenthesised reduction (the CSV + dashboard already net it).
 */
export type DocType = 'INVOICE' | 'CREDIT_NOTE';

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
  /**
   * INVOICE (a spend) or CREDIT_NOTE (a reduction). Backend register rows now
   * always carry this; treat a missing value as INVOICE for forward safety.
   */
  doc_type: DocType;
  /** For a CREDIT_NOTE, the invoice it reduces (chosen at upload), else null. */
  against_invoice_id: number | null;
  supplier_name: string | null;
  supplier_gstin: string | null;
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  invoice_date: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  grand_total_paise: number | null;
  /** Cost-allocation: the project this invoice is tagged to (chosen at upload). */
  project_id: number | null;
  /** The tagged project's `<CLIENT_CODE>-<n>` code, e.g. "BRI-001". */
  project_code: string | null;
  /** The tagged project's name, e.g. "General / Overhead". */
  project_name: string | null;
  /** Cost-allocation: the payment method this invoice is tagged to. */
  payment_method_id: number | null;
  payment_method_name: string | null;
  /** ISO datetime string (backend `created_at`). */
  created_at: string;
}

/** An admin-managed payment method (backend `PaymentMethodOut`). */
export interface PaymentMethod {
  id: number;
  name: string;
  active: boolean;
}

/** Create body for a payment method (Manage). */
export interface PaymentMethodInput {
  name: string;
}

/** Patch body for a payment method — rename and/or activate/deactivate (Manage). */
export interface PaymentMethodUpdate {
  name?: string;
  active?: boolean;
}

/** One project's confirmed-spend rollup (backend summary `by_project` row). A NULL
 * id/code is the "Unallocated" bucket (pre-inc-27 confirmed rows) that keeps the group
 * totals reconciling to the grand total. */
export interface ProjectSpend {
  project_id: number | null;
  project_code: string | null;
  project_name: string | null;
  /** PAISE. */
  total_paise: number;
  count: number;
}

/** One payment method's confirmed-spend rollup (backend summary `by_payment_method` row).
 * A NULL id/name is the "Unallocated" bucket. */
export interface PaymentMethodSpend {
  payment_method_id: number | null;
  name: string | null;
  /** PAISE. */
  total_paise: number;
  count: number;
}

/**
 * Confirmed-spend dashboard rollup (backend GET /expense/summary). Totals are over
 * CONFIRMED invoices only; money is integer PAISE on the wire.
 */
export interface ExpenseSummary {
  /** PAISE. */
  total_confirmed_paise: number;
  invoice_count: number;
  by_project: ProjectSpend[];
  by_payment_method: PaymentMethodSpend[];
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
  /** INVOICE (a spend) or CREDIT_NOTE (a reduction of a prior invoice). */
  doc_type: DocType;
  /** For a CREDIT_NOTE, the invoice it reduces, else null. */
  against_invoice_id: number | null;
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
  /** Restrict to one document kind (INVOICE / CREDIT_NOTE). */
  doc_type?: DocType | '';
  /** Inclusive lower bound on invoice_date (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound on invoice_date (YYYY-MM-DD). */
  date_to?: string;
  /** Cost-allocation filter: restrict to one project (id as a string for the query). */
  project_id?: string;
  /** Cost-allocation filter: restrict to one payment method (id as a string). */
  payment_method_id?: string;
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
  if (filters.doc_type) params.set('doc_type', filters.doc_type);
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
  if (filters.project_id?.trim()) params.set('project_id', filters.project_id.trim());
  if (filters.payment_method_id?.trim())
    params.set('payment_method_id', filters.payment_method_id.trim());
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
  paymentMethods: (activeOnly: boolean) => ['expense', 'payment-methods', activeOnly] as const,
  summary: ['expense', 'summary'] as const,
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

/**
 * The admin-managed payment methods list. `activeOnly` (→ `?active_only=true`) is
 * used by the upload picker (only choosable methods); the admin tab lists all.
 */
export function usePaymentMethods(activeOnly = false): UseQueryResult<PaymentMethod[], Error> {
  const { get } = useApi();
  return useQuery<PaymentMethod[], Error>({
    queryKey: expenseKeys.paymentMethods(activeOnly),
    queryFn: ({ signal }) =>
      get<PaymentMethod[]>(
        `/expense/payment-methods${activeOnly ? '?active_only=true' : ''}`,
        signal,
      ),
  });
}

/**
 * A flat list of existing INVOICE rows, for the credit-note "against invoice"
 * picker on the upload screen. Gated by `enabled` so it fires only once the
 * operator has actually chosen CREDIT_NOTE (an INVOICE upload never fetches it).
 * Filtered to `doc_type=INVOICE` — a credit note references an invoice, not
 * another credit note — and capped at a generous page for picker context.
 */
export function useInvoiceOptions(enabled: boolean): UseQueryResult<InvoiceOut[], Error> {
  const { get } = useApi();
  return useQuery<InvoiceOut[], Error>({
    queryKey: ['expense', 'invoice-options'],
    enabled,
    queryFn: ({ signal }) =>
      get<InvoiceOut[]>('/expense/invoices?doc_type=INVOICE&limit=200', signal),
  });
}

/** Confirmed-spend rollup for the Overview dashboard (by project + payment method). */
export function useExpenseSummary(): UseQueryResult<ExpenseSummary, Error> {
  const { get } = useApi();
  return useQuery<ExpenseSummary, Error>({
    queryKey: expenseKeys.summary,
    queryFn: ({ signal }) => get<ExpenseSummary>('/expense/summary', signal),
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
 * Arguments for a bulk upload. Every invoice in a batch is tagged with the SAME
 * `projectId` + `paymentMethodId` (chosen once, at upload, for the whole batch);
 * both are REQUIRED. Ids are strings because they originate from `<select>` values
 * and ride the multipart form as text (FastAPI coerces `project_id` / `payment_method_id`
 * to ints server-side).
 */
export interface UploadArgs {
  files: File[];
  projectId: string;
  paymentMethodId: string;
  /** The document kind for the whole batch (defaults to INVOICE at the call site). */
  docType: DocType;
  /**
   * For a CREDIT_NOTE batch, the optional invoice it is issued against (id as a
   * string from the `<select>`). Ignored / omitted for an INVOICE batch or when blank.
   */
  againstInvoiceId?: string;
}

/**
 * Bulk-upload N PDFs (one invoice each) as a single multipart batch under the
 * `files` field, tagged with a project + payment method. Returns the per-file
 * outcomes (incl. any DUPLICATE carrying the existing invoice's summary). The
 * delete-and-re-upload flow reuses this hook with a single-element array and the
 * same tags. Invalidates the register + summary on success.
 */
export function useUploadInvoices(): UseMutationResult<UploadBatchOut, Error, UploadArgs> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<UploadBatchOut, Error, UploadArgs>({
    mutationFn: ({ files, projectId, paymentMethodId, docType, againstInvoiceId }) => {
      const form = new FormData();
      for (const f of files) form.append('files', f);
      // project is OPTIONAL (a general expense): omit the field when none is chosen — the
      // backend's `int | None` cannot parse an empty-string form value.
      if (projectId?.trim()) form.append('project_id', projectId.trim());
      form.append('payment_method_id', paymentMethodId);
      form.append('doc_type', docType);
      // Only a credit note carries an against-invoice ref, and only when one was
      // actually picked — an INVOICE batch (or a blank pick) sends no field.
      if (docType === 'CREDIT_NOTE' && againstInvoiceId?.trim()) {
        form.append('against_invoice_id', againstInvoiceId.trim());
      }
      return postForm<UploadBatchOut>('/expense/invoices', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['expense', 'invoices'] });
      void qc.invalidateQueries({ queryKey: ['expense', 'summary'] });
    },
  });
}

/** Create a payment method (Manage). Invalidates the payment-methods list on success. */
export function useCreatePaymentMethod(): UseMutationResult<
  PaymentMethod,
  ApiError,
  PaymentMethodInput
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<PaymentMethod, ApiError, PaymentMethodInput>({
    mutationFn: (body) => post<PaymentMethod>('/expense/payment-methods', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['expense', 'payment-methods'] });
    },
  });
}

/**
 * Rename and/or activate-deactivate a payment method (Manage). No hard delete —
 * deactivation just hides it from the upload picker. Invalidates the list on success.
 */
export function useUpdatePaymentMethod(): UseMutationResult<
  PaymentMethod,
  ApiError,
  { id: number; body: PaymentMethodUpdate }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<PaymentMethod, ApiError, { id: number; body: PaymentMethodUpdate }>({
    mutationFn: ({ id, body }) => patch<PaymentMethod>(`/expense/payment-methods/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['expense', 'payment-methods'] });
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
