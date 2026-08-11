import { NavLink } from 'react-router-dom';

export interface TabDef {
  to: string;
  label: string;
  /** Match the route exactly (for index tabs). */
  end?: boolean;
}

/** Route-driven horizontal tab bar (uses NavLink active state). */
export function Tabs({ tabs }: { tabs: TabDef[] }) {
  return (
    <div className="mb-5 flex gap-1 border-b border-slate-200">
      {tabs.map((t) => (
        <NavLink
          key={t.to}
          to={t.to}
          end={t.end}
          className={({ isActive }) =>
            `-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              isActive
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-slate-500 hover:text-slate-800'
            }`
          }
        >
          {t.label}
        </NavLink>
      ))}
    </div>
  );
}
