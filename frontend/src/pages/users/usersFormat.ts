import { ApiError } from '../../api/client';

/** Shared formatting + label helpers for the User Management screens. */

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/** Display a user's role name, falling back to an em dash when unassigned. */
export function roleLabel(roleName: string | null): string {
  return roleName && roleName.length > 0 ? roleName : '—';
}

/** Localised datetime for an ISO string; em dash for null/blank, raw string if unparseable. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Basic email shape check (UI hint only; the server is authoritative). */
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export function isValidEmail(value: string): boolean {
  return EMAIL_RE.test(value.trim());
}
