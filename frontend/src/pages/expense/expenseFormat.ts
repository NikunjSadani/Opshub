import { ApiError } from '../../api/client';
import type {
  FieldStatus,
  InvoiceStatus,
  UploadResultStatus,
} from '../../api/expense';

/** Shared formatting + status helpers for the Expense / Invoice screens. */

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Route base for the module; imported by every screen so links stay consistent. */
export const EXPENSE_BASE = '/m/expense_invoice';

const INR = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * Format an integer PAISE amount as Indian rupees, or "—" when null/undefined.
 * Money is always integer paise on the wire (never float) — this is the single
 * paise→₹ presentation seam for the module.
 */
export function formatPaise(paise: number | null | undefined): string {
  if (paise == null) return '—';
  return INR.format(paise / 100);
}

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/** Sentence-case labels for a stored invoice's lifecycle status. */
export const INVOICE_STATUS_LABEL: Record<InvoiceStatus, string> = {
  UPLOADED: 'Uploaded',
  EXTRACTED: 'Extracted',
  NEEDS_REVIEW: 'Needs review',
  NEEDS_OCR: 'Needs OCR',
  CONFIRMED: 'Confirmed',
  REJECTED: 'Rejected',
};

export const INVOICE_STATUS_TONE: Record<InvoiceStatus, Tone> = {
  UPLOADED: 'slate',
  EXTRACTED: 'blue',
  NEEDS_REVIEW: 'amber',
  NEEDS_OCR: 'amber',
  CONFIRMED: 'green',
  REJECTED: 'red',
};

/** Labels + tones for a per-file upload outcome (adds the upload-only DUPLICATE). */
export const UPLOAD_STATUS_LABEL: Record<UploadResultStatus, string> = {
  EXTRACTED: 'Extracted',
  NEEDS_REVIEW: 'Needs review',
  NEEDS_OCR: 'Needs OCR',
  REJECTED: 'Rejected',
  DUPLICATE: 'Duplicate',
};

export const UPLOAD_STATUS_TONE: Record<UploadResultStatus, Tone> = {
  EXTRACTED: 'green',
  NEEDS_REVIEW: 'amber',
  NEEDS_OCR: 'slate',
  REJECTED: 'red',
  DUPLICATE: 'amber',
};

/** Tone for a single extracted field's confidence status. */
export const FIELD_STATUS_TONE: Record<FieldStatus, Tone> = {
  OK: 'green',
  CORRECTED: 'blue',
  LOW_CONFIDENCE: 'amber',
  MISSING: 'red',
};

export const FIELD_STATUS_LABEL: Record<FieldStatus, string> = {
  OK: 'OK',
  CORRECTED: 'Corrected',
  LOW_CONFIDENCE: 'Low confidence',
  MISSING: 'Missing',
};

/** Format a 0..1 confidence as a whole percent, or "—" when there is none. */
export function formatConfidence(confidence: number | null | undefined): string {
  if (confidence == null) return '—';
  return `${Math.round(confidence * 100)}%`;
}

/**
 * A readable label for a canonical `field_path`. Known header/totals paths get a
 * friendly name; a `line.<n>.<attr>` path is humanised generically; anything else
 * falls back to its last dotted segment, title-cased.
 */
export function fieldLabel(fieldPath: string): string {
  const known: Record<string, string> = {
    'header.supplier_name': 'Supplier name',
    'header.supplier_gstin': 'Supplier GSTIN',
    'header.supplier_address': 'Supplier address',
    'header.buyer_name': 'Buyer name',
    'header.buyer_gstin': 'Buyer GSTIN',
    'header.buyer_address': 'Buyer address',
    'header.invoice_number': 'Invoice number',
    'header.invoice_date': 'Invoice date',
    'header.place_of_supply': 'Place of supply',
    'header.po_ref': 'PO reference',
    'totals.total_taxable_paise': 'Total taxable',
    'totals.total_cgst_paise': 'Total CGST',
    'totals.total_sgst_paise': 'Total SGST',
    'totals.total_igst_paise': 'Total IGST',
    'totals.round_off_paise': 'Round off',
    'totals.grand_total_paise': 'Grand total',
    'totals.amount_in_words': 'Amount in words',
  };
  if (known[fieldPath]) return known[fieldPath];
  const line = /^line\.(\d+)\.(.+)$/.exec(fieldPath);
  if (line) {
    const attr = line[2].replace(/_paise$/, '').replace(/_/g, ' ');
    return `Line ${line[1]} · ${attr}`;
  }
  const seg = fieldPath.split('.').pop() ?? fieldPath;
  return seg.charAt(0).toUpperCase() + seg.slice(1).replace(/_/g, ' ');
}

/** Whether a field path names a money (paise) value — used for input hints. */
export function isMoneyField(fieldPath: string): boolean {
  return fieldPath.endsWith('_paise');
}

/**
 * Parse an operator-typed rupee string into integer PAISE, or null when it is not
 * a valid amount. Accepts an optional ₹, thousands separators, surrounding spaces,
 * an optional leading minus (round-off can be negative), and up to two decimals.
 * The backend parses money corrections as integer paise, so this is the single
 * ₹→paise seam for editable money fields (mirrors `formatPaise` in the read path).
 */
export function parseRupeesToPaise(input: string): number | null {
  const cleaned = input.replace(/[₹,\s]/g, '');
  if (!/^-?\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  return Math.round(parseFloat(cleaned) * 100);
}
