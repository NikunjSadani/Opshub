import { Outlet } from 'react-router-dom';
import { TopBar } from './TopBar';
import { Sidebar } from './Sidebar';
import { DevRoleSwitcher } from '../auth/DevRoleSwitcher';
import { usePermissions } from '../auth/AuthProvider';
import { Button } from '../ui';

/** Authenticated layout: top bar + left sidebar + routed content area. */
export function AppShell() {
  const perms = usePermissions();

  // Access (GET /me) is still loading: show a lightweight full-screen loader
  // rather than the chrome with a blank identity + empty nav that would flash
  // (and, on an /admin/* deep link, a misleading "no permission" page).
  if (perms.loading) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="grid h-screen place-items-center text-sm text-slate-400"
      >
        Loading your workspace…
      </div>
    );
  }

  // GET /me FAILED: the shell can't know the user's access, so render a clear,
  // actionable global state instead of empty nav + a lie ("You don't have the
  // iam permission") on any gated route. This is a load failure, not a denial —
  // Retry re-runs the /me fetch.
  if (perms.error) {
    return (
      <div className="grid h-screen place-items-center bg-slate-50 p-6 text-center">
        <div className="max-w-md">
          <h1 className="text-lg font-semibold text-slate-900">Couldn't load your access</h1>
          <p className="mt-2 text-sm text-slate-600">
            We couldn't load your permissions just now. This is a loading problem, not a
            permissions one — please try again.
          </p>
          <div className="mt-4 flex justify-center">
            <Button onClick={() => perms.refetch()}>Retry</Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      <div className="relative">
        <TopBar />
        {/* Dev-only role switcher, centered in the header band. Self-gates to
            dev builds under the mock provider; renders null otherwise. */}
        <div className="pointer-events-none absolute inset-x-0 top-0 flex h-14 items-center justify-center">
          <DevRoleSwitcher />
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
