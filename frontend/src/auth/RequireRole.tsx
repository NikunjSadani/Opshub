import type { ReactNode } from 'react';
import { useAuth, type Role } from './AuthProvider';

/**
 * Reusable, UX-only role guard.
 *
 * NOTE: server-side RBAC is the REAL gate — the backend authorizes every
 * request and serves each user only the modules they may access. This
 * component (and {@link useHasRole}) merely hides UI a user can't act on, to
 * avoid dead-ends. It is convenience, not security. (Same stance as the
 * Master Data tab check in `pages/challan/ChallanModule.tsx`.)
 */

/** True when the signed-in user's role is one of `roles`. */
export function useHasRole(...roles: Role[]): boolean {
  const { user } = useAuth();
  return user != null && roles.includes(user.role);
}

/** Small, friendly "you don't have access" panel for a role miss. */
function NotAuthorized({ allow }: { allow: Role[] }) {
  return (
    <div
      role="alert"
      className="mx-auto mt-10 max-w-md rounded-lg border border-amber-200 bg-amber-50 p-4 text-center"
    >
      <p className="text-sm font-semibold text-amber-800">Not authorized</p>
      <p className="mt-1 text-xs text-amber-700">
        This area is limited to: {allow.join(', ')}.
      </p>
    </div>
  );
}

export interface RequireRoleProps {
  /** Roles permitted to see `children`. */
  allow: Role[];
  children: ReactNode;
  /**
   * What to render on a role miss. Defaults to a visible "Not authorized"
   * panel (friendlier than a silent redirect for a role mismatch). Pass
   * `null` to render nothing (e.g. when guarding an optional nav item).
   */
  fallback?: ReactNode;
}

/**
 * Renders `children` only when the current user's role is in `allow`;
 * otherwise renders `fallback` (a "Not authorized" panel by default).
 *
 * @example
 * <RequireRole allow={['ADMIN']}>
 *   <DangerZone />
 * </RequireRole>
 */
export function RequireRole({ allow, children, fallback }: RequireRoleProps) {
  const ok = useHasRole(...allow);
  if (ok) return <>{children}</>;
  if (fallback !== undefined) return <>{fallback}</>;
  return <NotAuthorized allow={allow} />;
}
