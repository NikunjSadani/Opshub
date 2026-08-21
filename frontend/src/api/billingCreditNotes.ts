import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the client CREDIT-NOTE capture + match
 * slice of the Billing & AR module (module key `billing`). Mirrors the backend
 * app/modules/billing/creditnote_routes.py. All paths are relative to `/api/v1`
 * (added by `useApi()`). MONEY is integer PAISE on the wire.
 *
 * A credit note is a REDUCTION issued against a previously-confirmed sales invoice.
 * Its totals ride the wire as POSITIVE paise (the "credit amount"); the UI shows them
 * plainly but labels the document as a credit note.
 *
 * Ids are treated as strings (mirroring api/billingInvoices.ts + api/purchaseOrders.ts):
 * they only ever flow through `<select>` values, query strings, route params, and
 * equality checks — never arithmetic. FastAPI coerces numeric-string ids to ints.
 *
 * IMPORTANT: the detail shape is NESTED — a `referenced_invoice` object, a `source_file`
 * object, and a `lines` array — NOT a flattened row. Keep these interfaces in lock-step
 * with the backend `CNDetail` / `CNLine`, not with the lean list `CNOut`.
 */

// --- status unions ------------------------------------------------------------

/** A stored credit note's lifecycle status. Mirrors the sales-invoice lifecycle. */
export type CreditNoteStatus =
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
 * identity collides an existing credit note) to the in-review status set; a duplicate
 * is never persisted, so it is not a stored `CreditNoteStatus`.
 */
export type CnUploadOutcomeStatus =
  | 'EXTRACTED'
  | 'NEEDS_REVIEW'
  | 'NEEDS_OCR'
  | 'NEEDS_MATCH'
  | 'MATCHED'
  | 'REJECTED'
  | 'DUPLICATE';

/** One credit-note line's PO-match status (backend CNLine.match_status). */
export type CnLineMatchStatus = 'MATCHED' | 'MANUAL' | 'UNMATCHED';

/**
 * A single extracted CN field's confidence status. Structurally identical to the
 * invoice `FieldStatus` — the field-editor reuses the shared FIELD_STATUS_* maps.
 */
export type CnFieldStatus = 'OK' | 'LOW_CONFIDENCE' | 'MISSING' | 'CORRECTED';

// --- DTOs ---------------------------------------------------------------------

/**
 * A register row — EXACTLY the scalars the list endpoint returns (backend `CNOut`).
 * A lean projection: it does NOT carry the tax sub-totals, the referenced-invoice
 * object, the source file, or the lines — those live only on the detail response.
 * Keep in lock-step with the backend list `CNOut`, not `CNDetail`.
 */
export interface CreditNoteRow {
  id: string;
  cn_number: string | null;
  client_id: string;
  /** The invoice this CN credits. */
  invoice_id: string;
  /** The referenced invoice's number, denormalised onto the row for the register. */
  invoice_number: string | null;
  /** ISO date string (backend `date`); null when not extracted. */
  cn_date: string | null;
  /** PAISE — the credit amount (positive on the wire). */
  grand_total_paise: number | null;
  status: CreditNoteStatus;
}

/** The source PDF a credit note was extracted from (backend `CNDetail.source_file`). */
export interface CnSourceFile {
  id: string;
  filename: string;
}

/**
 * The confirmed sales invoice a credit note is issued against (backend
 * `CNDetail.referenced_invoice`). A CN can only be CONFIRMED once this invoice is
 * itself CONFIRMED — the review screen surfaces its status for that reason.
 */
export interface CnReferencedInvoice {
  id: string;
  invoice_number: string | null;
  po_id: string | null;
  /** PAISE. */
  grand_total_paise: number | null;
  status: string;
}

/**
 * One extracted CN header/total field envelope (backend `CNFieldOut`). Unlike the
 * invoice fields, the `field_path` is BARE (no `header.`/`totals.` prefix), one of:
 * cn_number, cn_date, total_taxable_paise, total_cgst_paise, total_sgst_paise,
 * total_igst_paise, round_off_paise, grand_total_paise. Money paths end in `_paise`.
 */
export interface CnFieldOut {
  field_path: string;
  /** Null when missing. Money values are integer-paise strings. */
  value_norm: string | null;
  value_raw: string | null;
  /** 0..1 calibrated confidence. */
  confidence: number;
  source_engine: string | null;
  status: CnFieldStatus;
}

/**
 * One line item on a credit note, with its PO-match state (backend `CNLine`). The
 * matched PO line is carried BOTH as an id AND as a ready-to-show `po_line_label`, so
 * the review table can render the current mapping without a second lookup.
 */
export interface CreditNoteLine {
  id: string;
  line_no: number;
  description: string | null;
  /** Decimal serialised as a string; null when absent. */
  quantity: string | null;
  /** PAISE. */
  taxable_paise: number | null;
  /** PAISE. */
  line_total_paise: number | null;
  /** The matched PO line item id, or null when UNMATCHED. */
  po_line_item_id: number | null;
  /** A ready-to-display label for the matched PO line, or null when UNMATCHED. */
  po_line_label: string | null;
  match_status: CnLineMatchStatus;
  /**
   * The referenced invoice line's billed quantity (Decimal string), for the
   * over-credit tally. Null when the line is UNMATCHED (no invoice line to compare to).
   */
  billed_qty: string | null;
  /**
   * Quantity already credited on this invoice line by OTHER confirmed credit notes
   * (Decimal string). Null when the line is UNMATCHED. This CN's own `quantity` is
   * added on top to get the credited-so-far total.
   */
  already_credited_qty: string | null;
}

/**
 * Full credit-note detail (backend `CNDetail`, GET /billing/credit-notes/{id} and the
 * PATCH review / match responses). NESTED: `referenced_invoice`, `source_file`, and the
 * per-line items with their match state — plus the tax sub-totals + round-off + reason.
 */
export interface CreditNoteDetail {
  id: string;
  cn_number: string | null;
  client_id: string;
  invoice_id: string;
  /** ISO date string (backend `date`); null when not extracted. */
  cn_date: string | null;
  /** PAISE. */
  total_taxable_paise: number | null;
  total_cgst_paise: number | null;
  total_sgst_paise: number | null;
  total_igst_paise: number | null;
  /** PAISE, SIGNED (round-off can be negative). */
  round_off_paise: number | null;
  grand_total_paise: number | null;
  status: CreditNoteStatus;
  /** True when the source PDF had no text layer (scanned/image) — fields not auto-extracted. */
  needs_ocr: boolean;
  /** Human-readable reasons this CN was flagged for review (amber banner). */
  review_reasons: string[];
  /** Per-field extraction envelopes for the header + totals (the review field-editor). */
  fields: CnFieldOut[];
  /** Why the credit note was issued (free text), or null. */
  reason: string | null;
  source_file: CnSourceFile | null;
  referenced_invoice: CnReferencedInvoice | null;
  lines: CreditNoteLine[];
}

/**
 * One file's outcome inside an upload batch (backend credit-note `outcomes[]`). Leaner
 * than the invoice outcome: on a stored/duplicate file `cn_id` / `cn_number` describe the
 * resulting (or existing) credit note. Outcomes are index-aligned with the files posted,
 * so a row's display name comes from the local File at the same position.
 */
export interface CnUploadOutcome {
  file_id: string;
  status: CnUploadOutcomeStatus;
  /** The stored (or existing) credit note's id; null on a REJECTED file. */
  cn_id: string | null;
  cn_number: string | null;
}

/** The response of a bulk upload (backend POST /billing/credit-notes). */
export interface CnUploadBatch {
  outcomes: CnUploadOutcome[];
}

/**
 * One field correction submitted from the review panel (backend `CNCorrectionItem`).
 * An ARRAY of these rides the wire under `corrections`. NOTE the wire key is `field`
 * (= the envelope's `field_path`), NOT `field_path`. For a money (`*_paise`) field the
 * `value` MUST be an integer-paise string (the backend coerces money corrections to
 * int); the review screen converts the operator's rupee input to paise before building it.
 */
export interface CnCorrection {
  field: string;
  value: string;
}

export interface CreditNoteFilters {
  status?: CreditNoteStatus | '';
  /** Restrict to one client (id as a string for the query). */
  client_id?: string;
  /** Restrict to credit notes issued against one invoice (id as a string). */
  invoice_id?: string;
}

// --- query-string builder (pure, unit-testable) -------------------------------

/**
 * Build the `?status=&client_id=&invoice_id=` query for the register list, appending
 * only non-empty (trimmed) params.
 */
export function buildCreditNoteListQuery(filters: CreditNoteFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set('status', filters.status);
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.invoice_id?.trim()) params.set('invoice_id', filters.invoice_id.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const creditNoteKeys = {
  all: ['billing', 'credit-notes'] as const,
  list: (filters: CreditNoteFilters) => ['billing', 'credit-notes', filters] as const,
  // Normalise the id to a string so the detail query (route param = string) and every
  // mutation's setQueryData / invalidation land on the SAME cache key regardless of
  // whether the backend serialised the id as a number (a Wave-2 lesson).
  detail: (id: string | number) => ['billing', 'credit-note', String(id)] as const,
};

/**
 * A CONFIRMED client credit note reduces cross-module reads the CN register alone doesn't
 * cover: the referenced invoice's AR (credited / outstanding), the invoice register + detail,
 * the PO line invoiced-qty, and project P&L. Refresh those too so an already-mounted AR /
 * invoice / PO view doesn't show pre-credit numbers (refetchOnWindowFocus is off app-wide).
 * Broad-but-cheap prefix invalidations; read-refresh only, no mutation.
 */
function invalidateBillingCrossModule(qc: ReturnType<typeof useQueryClient>): void {
  void qc.invalidateQueries({ queryKey: ['billing', 'ar'] });        // AR register + details
  void qc.invalidateQueries({ queryKey: ['billing', 'invoices'] });  // invoice register (list)
  void qc.invalidateQueries({ queryKey: ['billing', 'invoice'] });   // open invoice detail
  void qc.invalidateQueries({ queryKey: ['purchase-orders'] });      // PO detail invoiced-qty + list
}

// --- queries ------------------------------------------------------------------

/** The credit-note register, filtered by status / client / referenced invoice (VIEW). */
export function useCreditNotesQuery(
  filters: CreditNoteFilters,
): UseQueryResult<CreditNoteRow[], Error> {
  const { get } = useApi();
  return useQuery<CreditNoteRow[], Error>({
    queryKey: creditNoteKeys.list(filters),
    queryFn: ({ signal }) =>
      get<CreditNoteRow[]>(`/billing/credit-notes${buildCreditNoteListQuery(filters)}`, signal),
  });
}

/** A single credit note with its referenced invoice, source file, and lines. */
export function useCreditNoteQuery(
  id: string | null,
): UseQueryResult<CreditNoteDetail, Error> {
  const { get } = useApi();
  return useQuery<CreditNoteDetail, Error>({
    queryKey: creditNoteKeys.detail(id ?? ''),
    enabled: id != null && id !== '',
    queryFn: ({ signal }) => get<CreditNoteDetail>(`/billing/credit-notes/${id}`, signal),
  });
}

// --- mutations ----------------------------------------------------------------

/**
 * Arguments for a bulk upload. `invoiceId` is REQUIRED — every credit note is issued
 * against exactly one (CONFIRMED) sales invoice, and the whole batch is tagged to it.
 * Ids are strings because they originate from a `<select>` value and ride the multipart
 * form as text.
 */
export interface CnUploadArgs {
  files: File[];
  invoiceId: string;
}

/**
 * Bulk-upload N credit-note PDFs (one CN each) as a single multipart batch under the
 * `file` field (repeated), tagged with the invoice they credit. Returns the per-file
 * outcomes (incl. any DUPLICATE / REJECTED). Invalidates the register on success.
 */
export function useUploadCreditNotes(): UseMutationResult<
  CnUploadBatch,
  ApiError,
  CnUploadArgs
> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<CnUploadBatch, ApiError, CnUploadArgs>({
    mutationFn: ({ files, invoiceId }) => {
      const form = new FormData();
      for (const f of files) form.append('file', f);
      form.append('invoice_id', invoiceId);
      return postForm<CnUploadBatch>('/billing/credit-notes', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
    },
  });
}

/**
 * Submit header/total corrections for a credit note, optionally confirming it (OPERATE).
 * On `confirm: true` the backend 409s unless the referenced invoice is CONFIRMED AND
 * every line is matched. Seeds the detail cache and refreshes the register on success.
 */
export function useSubmitCnReview(): UseMutationResult<
  CreditNoteDetail,
  ApiError,
  { id: string; corrections?: CnCorrection[]; confirm?: boolean }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    CreditNoteDetail,
    ApiError,
    { id: string; corrections?: CnCorrection[]; confirm?: boolean }
  >({
    // Send `corrections` only when there is something to correct — a bare confirm
    // posts just `{ confirm: true }` (the backend treats a missing key as no edits).
    mutationFn: ({ id, corrections, confirm }) =>
      patch<CreditNoteDetail>(`/billing/credit-notes/${id}/review`, {
        ...(corrections && corrections.length > 0 ? { corrections } : {}),
        confirm: confirm ?? false,
      }),
    onSuccess: (cn) => {
      qc.setQueryData(creditNoteKeys.detail(cn.id), cn);
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
      // Only a CONFIRM writes back to AR / invoice / PO — a corrections-only save does not,
      // so don't churn those reads mid-review.
      if (cn.status === 'CONFIRMED') invalidateBillingCrossModule(qc);
    },
  });
}

/**
 * Re-run the auto-matcher over the credit note's still-unmatched lines (OPERATE). Seeds
 * the detail cache (the response carries the updated line match state) and refreshes the
 * register (the derived status may change).
 */
export function useRematchCn(): UseMutationResult<CreditNoteDetail, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<CreditNoteDetail, ApiError, string>({
    mutationFn: (id) => post<CreditNoteDetail>(`/billing/credit-notes/${id}/match`),
    onSuccess: (cn) => {
      qc.setQueryData(creditNoteKeys.detail(cn.id), cn);
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
    },
  });
}

/**
 * Manually map one credit-note line to a PO line item (OPERATE, status → MANUAL). Seeds
 * the detail cache with the response's fresh line state.
 */
export function useManualCnMatch(): UseMutationResult<
  CreditNoteDetail,
  ApiError,
  { id: string; lineId: string; poLineItemId: string }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    CreditNoteDetail,
    ApiError,
    { id: string; lineId: string; poLineItemId: string }
  >({
    mutationFn: ({ id, lineId, poLineItemId }) =>
      patch<CreditNoteDetail>(`/billing/credit-notes/${id}/lines/${lineId}/match`, {
        po_line_item_id: Number(poLineItemId),
      }),
    onSuccess: (cn) => {
      qc.setQueryData(creditNoteKeys.detail(cn.id), cn);
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
    },
  });
}

/** Soft-cancel an in-review credit note (MANAGE). 409 on a confirmed/cancelled one. */
export function useCancelCn(): UseMutationResult<CreditNoteDetail, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<CreditNoteDetail, ApiError, string>({
    mutationFn: (id) => post<CreditNoteDetail>(`/billing/credit-notes/${id}/cancel`),
    onSuccess: (cn) => {
      qc.setQueryData(creditNoteKeys.detail(cn.id), cn);
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
      invalidateBillingCrossModule(qc);  // reverses any AR/PO effect if this CN had one
    },
  });
}

/**
 * Delete a credit note + its source blob (MANAGE, 200 with `{id, deleted}`). Used by the
 * detail screen's Delete action. Invalidates the register on success.
 */
export function useDeleteCn(): UseMutationResult<
  { id: number; deleted: boolean },
  ApiError,
  string
> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<{ id: number; deleted: boolean }, ApiError, string>({
    mutationFn: (id) => del<{ id: number; deleted: boolean }>(`/billing/credit-notes/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: creditNoteKeys.all });
      invalidateBillingCrossModule(qc);  // reverses any AR/PO effect if this CN had one
    },
  });
}
