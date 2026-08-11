import { ApiError } from '../../api/client';
import type { BatchStatus, ChallanStatus } from '../../api/challan';

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
