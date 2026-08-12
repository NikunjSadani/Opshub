import { Outlet } from 'react-router-dom';
import { TopBar } from './TopBar';
import { Sidebar } from './Sidebar';
import { DevRoleSwitcher } from '../auth/DevRoleSwitcher';

/** Authenticated layout: top bar + left sidebar + routed content area. */
export function AppShell() {
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
