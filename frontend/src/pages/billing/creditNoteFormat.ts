import type {
  CnUploadOutcomeStatus,
  CreditNoteStatus,
} from '../../api/billingCreditNotes';

/**
 * CN-specific status label + tone maps for the credit-note capture screens. Money,
 * date, error, and line-match formatting are shared with the invoice module and are
 * imported from `./billingInvoiceFormat` at the call sites — this file only owns the
 * credit-note lifecycle + upload-outcome vocab.
 */

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Sentence-case labels for a stored credit note's lifecycle status. */
export const CN_STATUS_LABEL: Record<CreditNoteStatus, string> = {
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

export const CN_STATUS_TONE: Record<CreditNoteStatus, Tone> = {
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
export const CN_UPLOAD_STATUS_LABEL: Record<CnUploadOutcomeStatus, string> = {
  EXTRACTED: 'Extracted',
  NEEDS_REVIEW: 'Needs review',
  NEEDS_OCR: 'Needs OCR',
  NEEDS_MATCH: 'Needs match',
  MATCHED: 'Matched',
  REJECTED: 'Rejected',
  DUPLICATE: 'Duplicate',
};

export const CN_UPLOAD_STATUS_TONE: Record<CnUploadOutcomeStatus, Tone> = {
  EXTRACTED: 'green',
  NEEDS_REVIEW: 'amber',
  NEEDS_OCR: 'slate',
  NEEDS_MATCH: 'amber',
  MATCHED: 'green',
  REJECTED: 'red',
  DUPLICATE: 'amber',
};

/**
 * A readable label for a canonical CN `field_path`. Unlike the invoice fields, CN
 * field paths are BARE (no `header.`/`totals.` prefix) — this owns that vocab.
 */
export function cnFieldLabel(fieldPath: string): string {
  const known: Record<string, string> = {
    cn_number: 'Credit note number',
    cn_date: 'Credit note date',
    total_taxable_paise: 'Total taxable',
    total_cgst_paise: 'Total CGST',
    total_sgst_paise: 'Total SGST',
    total_igst_paise: 'Total IGST',
    round_off_paise: 'Round off',
    grand_total_paise: 'Credit total',
  };
  if (known[fieldPath]) return known[fieldPath];
  const seg = fieldPath.split('.').pop() ?? fieldPath;
  return (
    seg.charAt(0).toUpperCase() + seg.slice(1).replace(/_paise$/, '').replace(/_/g, ' ')
  );
}

/** Which review card a CN field belongs in: the identifiers vs the money totals. */
export function cnFieldSection(fieldPath: string): 'header' | 'totals' {
  return fieldPath === 'cn_number' || fieldPath === 'cn_date' ? 'header' : 'totals';
}

/** A readable badge label + tone for the referenced invoice's status (free-text on the wire). */
export function referencedInvoiceTone(status: string): Tone {
  switch (status) {
    case 'CONFIRMED':
      return 'green';
    case 'CANCELLED':
    case 'REJECTED':
      return 'red';
    default:
      return 'amber';
  }
}
