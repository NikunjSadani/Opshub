import { ApiError } from '../../api/client';
import type { ProjectStatus } from '../../api/projects';

/** Shared formatting + status helpers for the Projects screens. */

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/**
 * Format a plain YYYY-MM-DD (or ISO datetime) date by slicing the date parts
 * directly, so a UTC parse can't shift it a day in timezones behind UTC.
 * Returns "—" for a null/empty value.
 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Human labels for the status union. */
export const PROJECT_STATUS_LABEL: Record<ProjectStatus, string> = {
  ACTIVE: 'Active',
  ON_HOLD: 'On hold',
  CLOSED: 'Closed',
};

export const PROJECT_STATUS_TONE: Record<ProjectStatus, Tone> = {
  ACTIVE: 'green',
  ON_HOLD: 'amber',
  CLOSED: 'slate',
};
