import { useMockDevControls } from './AuthProvider';

/**
 * LOCAL dev-only role switcher.
 *
 * Lets you exercise every role-gated surface against the mock provider without
 * real Firebase. It renders NOTHING unless BOTH are true:
 *   1. the build is dev (`import.meta.env.DEV`), and
 *   2. the mock provider is active (so {@link useMockDevControls} is non-null).
 *
 * Under the real Firebase provider `useMockDevControls()` returns null and this
 * component disappears — the switching mechanism never leaks into production.
 * Role comes from a signed custom claim there; there is no client-side setter,
 * which is exactly why this lives outside the `AuthContextValue` contract.
 */
export function DevRoleSwitcher() {
  const dev = useMockDevControls();

  if (!import.meta.env.DEV || !dev) return null;

  return (
    <label className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-800 shadow-sm">
      <span className="uppercase tracking-wide text-amber-500">Role (dev)</span>
      <select
        aria-label="Dev role switcher"
        value={dev.role}
        onChange={(e) => dev.setRole(e.target.value as (typeof dev.roles)[number])}
        className="rounded bg-transparent text-[11px] font-semibold text-amber-900 focus:outline-none"
      >
        {dev.roles.map((r) => (
          <option key={r} value={r}>
            {r}
          </option>
        ))}
      </select>
    </label>
  );
}
