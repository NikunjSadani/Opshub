import { Link } from 'react-router-dom';
import { useModules } from '../api/client';
import { ErrorState } from '../ui';
import type { ModuleDescriptor } from '../types/modules';

function ModuleTile({ module }: { module: ModuleDescriptor }) {
  const base =
    'group relative flex h-32 flex-col justify-between rounded-xl border p-4 transition';

  if (module.coming_soon) {
    return (
      <div
        aria-disabled="true"
        className={`${base} cursor-not-allowed border-slate-200 bg-slate-50`}
      >
        <div>
          <p className="text-sm font-semibold text-slate-400">{module.title}</p>
          <p className="mt-1 text-xs text-slate-400">{module.nav_group}</p>
        </div>
        <span className="w-fit rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-500">
          Coming Soon
        </span>
      </div>
    );
  }

  return (
    <Link
      to={`/m/${module.key}`}
      className={`${base} border-slate-200 bg-white hover:border-brand-500 hover:shadow-sm`}
    >
      <div>
        <p className="text-sm font-semibold text-slate-900">{module.title}</p>
        <p className="mt-1 text-xs text-slate-500">{module.nav_group}</p>
      </div>
      <span className="text-sm font-medium text-brand-600 group-hover:text-brand-700">
        Open →
      </span>
    </Link>
  );
}

export function Dashboard() {
  const { data: modules, isLoading, isError, error, refetch } = useModules();

  return (
    <div>
      <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
      <p className="mt-1 text-sm text-slate-500">
        Operations modules available to you.
      </p>

      {isLoading && (
        <p className="mt-8 text-sm text-slate-400">Loading modules…</p>
      )}
      {isError && (
        <div className="mt-8">
          <ErrorState error={error} onRetry={() => void refetch()} />
        </div>
      )}

      {modules && modules.length === 0 && (
        <p className="mt-8 text-sm text-slate-500">
          No modules are available for your account.
        </p>
      )}

      {modules && modules.length > 0 && (
        <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {modules.map((m) => (
            <ModuleTile key={m.key} module={m} />
          ))}
        </div>
      )}
    </div>
  );
}
