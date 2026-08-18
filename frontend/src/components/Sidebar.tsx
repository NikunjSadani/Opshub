import { NavLink } from 'react-router-dom';
import { useModules } from '../api/client';
import { useAuth } from '../auth/AuthProvider';
import type { ModuleDescriptor } from '../types/modules';

function groupByNav(modules: ModuleDescriptor[]): [string, ModuleDescriptor[]][] {
  const groups = new Map<string, ModuleDescriptor[]>();
  for (const m of modules) {
    const list = groups.get(m.nav_group) ?? [];
    list.push(m);
    groups.set(m.nav_group, list);
  }
  return [...groups.entries()];
}

export function Sidebar() {
  const { data: modules, isLoading, isError } = useModules();
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';

  return (
    <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white md:block">
      <nav className="flex flex-col gap-6 px-3 py-4">
        <NavLink
          to="/"
          end
          className={({ isActive }) =>
            `flex items-center rounded-md px-3 py-2 text-sm font-medium ${
              isActive ? 'bg-brand-50 text-brand-700' : 'text-slate-700 hover:bg-slate-100'
            }`
          }
        >
          Dashboard
        </NavLink>

        {isLoading && (
          <p className="px-3 text-xs text-slate-400">Loading modules…</p>
        )}
        {isError && (
          <p className="px-3 text-xs text-rose-500">Failed to load modules</p>
        )}

        {modules &&
          groupByNav(modules).map(([group, items]) => (
            <div key={group}>
              <p className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                {group}
              </p>
              <div className="flex flex-col gap-0.5">
                {items.map((m) =>
                  m.coming_soon ? (
                    <span
                      key={m.key}
                      aria-disabled="true"
                      className="flex cursor-not-allowed items-center justify-between rounded-md px-3 py-2 text-sm text-slate-400"
                    >
                      {m.title}
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-slate-400">
                        Soon
                      </span>
                    </span>
                  ) : (
                    <NavLink
                      key={m.key}
                      to={`/m/${m.key}`}
                      className={({ isActive }) =>
                        `rounded-md px-3 py-2 text-sm font-medium ${
                          isActive
                            ? 'bg-brand-50 text-brand-700'
                            : 'text-slate-700 hover:bg-slate-100'
                        }`
                      }
                    >
                      {m.title}
                    </NavLink>
                  ),
                )}
              </div>
            </div>
          ))}

        {/* Admin-only surfaces. Hidden for non-admins (the backend enforces the
            real gate); showing a tile they can't use would be a dead-end. */}
        {isAdmin && (
          <div>
            <p className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              Administration
            </p>
            <div className="flex flex-col gap-0.5">
              <NavLink
                to="/admin/users"
                className={({ isActive }) =>
                  `rounded-md px-3 py-2 text-sm font-medium ${
                    isActive
                      ? 'bg-brand-50 text-brand-700'
                      : 'text-slate-700 hover:bg-slate-100'
                  }`
                }
              >
                Users
              </NavLink>
            </div>
          </div>
        )}
      </nav>
    </aside>
  );
}
