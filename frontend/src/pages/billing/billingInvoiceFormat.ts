import { ApiError } from '../../api/client';
import { rupees } from './billingFormat';
import type {
  FieldStatus,
  LineMatchStatus,
  SalesInvoiceStatus,
  UploadOutcomeStatus,
} from '../../api/billingInvoices';

/** Shared formatting + status helpers for the client-invoice capture screens. */

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/**
 * Format an integer PAISE amount as Indian rupees, or "—" when null/undefined. Money
 * is always integer paise on the wire — this is the single null-safe paise→₹ seam for
 * the module (delegates to `rupees()` for the non-null formatting).
 */
export function money(paise: number | null | undefined): string {
  if (paise == null) return '—';
  return rupees(paise);
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
export const INVOICE_STATUS_LABEL: Record<SalesInvoiceStatus, string> = {
  UPLOADED: 'Uploaded',
  EXTRACTED: 'Extracted',
  NEEDS_REVIEW: 'Needs review',
  NEEDS_OCR: 'Needs OCR',
  NEEDS_MATCH: 'Needs match',
  MATCHED: 'Matched',
  CONFIRMED: 'Confirmed',
  REJECTED: 'Rejected',
  CANCELLED: 'Cancelled',
};

export const INVOICE_STATUS_TONE: Record<SalesInvoiceStatus, Tone> = {
  UPLOADED: 'slate',
  EXTRACTED: 'blue',
  NEEDS_REVIEW: 'amber',
  NEEDS_OCR: 'amber',
  NEEDS_MATCH: 'amber',
  MATCHED: 'blue',
  CONFIRMED: 'green',
  REJECTED: 'red',
  CANCELLED: 'slate',
};

/** Labels + tones for a per-file upload outcome (adds the upload-only DUPLICATE). */
export const UPLOAD_STATUS_LABEL: Record<UploadOutcomeStatus, string> = {
  EXTRACTED: 'Extracted',
  NEEDS_REVIEW: 'Needs review',
  NEEDS_OCR: 'Needs OCR',
  NEEDS_MATCH: 'Needs match',
  MATCHED: 'Matched',
  REJECTED: 'Rejected',
  DUPLICATE: 'Duplicate',
};

export const UPLOAD_STATUS_TONE: Record<UploadOutcomeStatus, Tone> = {
  EXTRACTED: 'green',
  NEEDS_REVIEW: 'amber',
  NEEDS_OCR: 'slate',
  NEEDS_MATCH: 'amber',
  MATCHED: 'green',
  REJECTED: 'red',
  DUPLICATE: 'amber',
};

/** Distinct tones for a line's PO-match status (MATCHED/MANUAL/UNMATCHED read apart). */
export const MATCH_STATUS_LABEL: Record<LineMatchStatus, string> = {
  UNMATCHED: 'Unmatched',
  MATCHED: 'Matched',
  MANUAL: 'Manual',
};

export const MATCH_STATUS_TONE: Record<LineMatchStatus, Tone> = {
  UNMATCHED: 'red',
  MATCHED: 'green',
  MANUAL: 'blue',
};

/** Tone + label for a single extracted field's confidence status. */
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
 * The canonical fields that must be present (not MISSING / LOW_CONFIDENCE) before an
 * invoice can be CONFIRMED. Mirrors the backend REQUIRED_FIELD_PATHS. Note the sales
 * invoice keys on the BUYER GSTIN (the client being billed), not a supplier GSTIN.
 */
export const REQUIRED_FIELD_PATHS: readonly string[] = [
  'header.buyer_gstin',
  'header.invoice_number',
  'header.invoice_date',
  'totals.total_taxable_paise',
  'totals.grand_total_paise',
];

/** A readable label for a canonical `field_path`. */
export function fieldLabel(fieldPath: string): string {
  const known: Record<string, string> = {
    'header.supplier_gstin': 'Supplier GSTIN',
    'header.buyer_gstin': 'Buyer GSTIN',
    'header.invoice_number': 'Invoice number',
    'header.invoice_date': 'Invoice date',
    'header.due_date': 'Due date',
    'header.po_ref': 'PO reference',
    'totals.total_taxable_paise': 'Total taxable',
    'totals.total_cgst_paise': 'Total CGST',
    'totals.total_sgst_paise': 'Total SGST',
    'totals.total_igst_paise': 'Total IGST',
    'totals.round_off_paise': 'Round off',
    'totals.grand_total_paise': 'Grand total',
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

/** Whether a field path names a money (paise) value — used for input hints/parsing. */
export function isMoneyField(fieldPath: string): boolean {
  return fieldPath.endsWith('_paise');
}

/**
 * Parse an operator-typed rupee string into integer PAISE, or null when it is not a
 * valid amount. Accepts an optional ₹, thousands separators, surrounding spaces, an
 * optional leading minus (round-off can be negative), and up to two decimals. The
 * backend coerces money corrections to integer paise, so this is the single ₹→paise
 * write seam for editable money fields (mirrors `money()` on the read path).
 */
export function parseRupeesToPaise(input: string): number | null {
  const cleaned = input.replace(/[₹,\s]/g, '');
  if (!/^-?\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  return Math.round(parseFloat(cleaned) * 100);
}
