import { useParams } from 'react-router-dom';
import { useModules } from '../api/client';

/**
 * Generic landing shell for a module route (`/m/:key`). Real module UIs land
 * here as they are built; for now it confirms the route + module resolve.
 */
export function ModulePlaceholder() {
  const { key } = useParams<{ key: string }>();
  const { data: modules } = useModules();
  const module = modules?.find((m) => m.key === key);

  return (
    <div>
      <h1 className="text-xl font-semibold text-slate-900">
        {module?.title ?? key}
      </h1>
      <p className="mt-1 text-sm text-slate-500">
        {module?.nav_group ?? 'Module'}
      </p>
      <div className="mt-6 rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        This module UI has not been built yet.
      </div>
    </div>
  );
}
