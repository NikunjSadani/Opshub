import type { ReactNode } from 'react';
import { usePermissions } from './AuthProvider';

/**
 * Reusable, UX-only permission guard (RBAC v2).
 *
 * NOTE: server-side RBAC is the REAL gate — the backend authorizes every
 * request and serves each user only the modules/actions they may access. This
 * component merely hides UI a user can't act on, to avoid dead-ends. It is
 * convenience, not security. (Same stance as the Master Data tab check in
 * `pages/challan/ChallanModule.tsx`.)
 */

/** Small, friendly "you don't have access" panel for a permission miss. */
function NotAuthorized({ perm }: { perm: string }) {
  return (
    <div
      role="alert"
      className="mx-auto mt-10 max-w-md rounded-lg border border-amber-200 bg-amber-50 p-4 text-center"
    >
      <p className="text-sm font-semibold text-amber-800">Not authorized</p>
      <p className="mt-1 text-xs text-amber-700">
        You don't have the <span className="font-medium">{perm}</span> permission for this area.
      </p>
    </div>
  );
}

export interface RequirePlatformProps {
  /** Platform permission required to see `children` (e.g. "iam", "settings"). */
  perm: string;
  children: ReactNode;
  /**
   * What to render on a permission miss. Defaults to a visible "Not authorized"
   * panel (friendlier than a silent redirect for a permission mismatch). Pass
   * `null` to render nothing (e.g. when guarding an optional nav item).
   */
  fallback?: ReactNode;
}

/**
 * Renders `children` only when the current user holds the platform permission
 * `perm`; otherwise renders `fallback` (a "Not authorized" panel by default).
 * While permissions are still loading, renders nothing to avoid flashing the
 * not-authorized panel before `GET /me` resolves.
 *
 * @example
 * <RequirePlatform perm="iam">
 *   <UsersModule />
 * </RequirePlatform>
 */
export function RequirePlatform({ perm, children, fallback }: RequirePlatformProps) {
  const perms = usePermissions();
  if (perms.loading) {
    return (
      <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
    );
  }
  if (perms.hasPlatform(perm)) return <>{children}</>;
  if (fallback !== undefined) return <>{fallback}</>;
  return <NotAuthorized perm={perm} />;
}
