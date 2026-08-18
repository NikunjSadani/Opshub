import { ApiError } from '../../api/client';
import type { Role } from '../../api/users';

/** Shared formatting + label helpers for the User Management screens. */

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/** Human labels for the role union. */
export const ROLE_LABEL: Record<Role, string> = {
  ADMIN: 'Admin',
  MIS: 'MIS',
  OPERATIONS: 'Operations',
  FINANCE: 'Finance',
};

/** Basic email shape check (UI hint only; the server is authoritative). */
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export function isValidEmail(value: string): boolean {
  return EMAIL_RE.test(value.trim());
}
