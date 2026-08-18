import { ErrorState, Loading } from '../../ui';
import { useAssignableModules } from '../../api/users';

/**
 * Grantable-module checkbox group used by the invite + edit forms. Reads the
 * assignable modules (system/coming-soon/health already filtered out) and
 * renders one checkbox per module. Controlled: the parent owns `selected`.
 */
export function ModuleCheckboxes({
  selected,
  onToggle,
  disabled = false,
}: {
  selected: string[];
  onToggle: (key: string, checked: boolean) => void;
  disabled?: boolean;
}) {
  const query = useAssignableModules();

  if (query.isPending) return <Loading label="Loading modules…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (query.data.length === 0) {
    return <p className="text-xs text-slate-400">No grantable modules are available.</p>;
  }

  return (
    <fieldset className="space-y-2">
      <legend className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
        Module access
      </legend>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {query.data.map((m) => (
          <label
            key={m.key}
            className="flex items-center gap-2 rounded-md border border-slate-200 px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            <input
              type="checkbox"
              checked={selected.includes(m.key)}
              disabled={disabled}
              onChange={(e) => onToggle(m.key, e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500/40"
            />
            <span className="min-w-0 truncate">{m.title}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
