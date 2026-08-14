import { ApiError } from '../../api/client';
import type { BatchOut, BatchStatus, ChallanStatus } from '../../api/challan';
import type { AllocationStatus } from '../../api/numbering';

/** Shared formatting + status helpers for the challan screens. */

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

const INR = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * Format an integer PAISE amount as Indian rupees, or "—" when null
 * (a value-free challan). Never renders "0" for an absent value.
 */
export function formatPaise(paise: number | null | undefined): string {
  if (paise == null) return '—';
  return INR.format(paise / 100);
}

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/** Download descriptors for whatever artifacts a batch has produced (shared by
 *  the Batches tab and the New Challan recent-batches list). */
export function batchArtifacts(
  b: BatchOut,
): Array<{ label: string; fileId: number; name: string }> {
  const links: Array<{ label: string; fileId: number; name: string }> = [];
  if (b.error_report_file_id != null) {
    // The same file id holds ERRORS on a failed-validation batch but non-blocking
    // WARNINGS on any batch that validated (VALIDATED/GENERATING/COMPLETED). Label
    // it by status so a succeeded batch never shows a misleading "Error report"
    // that an operator would skip — the deviations/possible-splits matter on a
    // statutory document. Terminology matches the report's own Severity column.
    const isErrors = b.status === 'FAILED_VALIDATION';
    links.push({
      label: isErrors ? 'Error report' : 'Warnings',
      fileId: b.error_report_file_id,
      name: `batch-${b.id}-${isErrors ? 'errors' : 'warnings'}.csv`,
    });
  }
  if (b.zip_file_id != null)
    links.push({ label: 'ZIP', fileId: b.zip_file_id, name: `batch-${b.id}.zip` });
  if (b.merged_pdf_file_id != null)
    links.push({ label: 'Merged PDF', fileId: b.merged_pdf_file_id, name: `batch-${b.id}.pdf` });
  return links;
}

export const BATCH_STATUS_TONE: Record<BatchStatus, Tone> = {
  PENDING: 'slate',
  FAILED_VALIDATION: 'red',
  VALIDATED: 'blue',
  GENERATING: 'amber',
  COMPLETED: 'green',
  FAILED: 'red',
};

export const CHALLAN_STATUS_TONE: Record<ChallanStatus, Tone> = {
  ISSUED: 'green',
  VOID: 'red',
};

export const ALLOCATION_STATUS_TONE: Record<AllocationStatus, Tone> = {
  RESERVED: 'amber',
  ISSUED: 'green',
  VOID: 'slate',
};
