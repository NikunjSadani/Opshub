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
 * It picks which SEEDED user to act as; the backend dev-auth shim resolves that
 * user (via `X-Dev-Uid`) and `GET /me` returns their real permissions, so both the
 * UI gating and the server enforcement reflect the selected role consistently.
 */
export function DevRoleSwitcher() {
  const dev = useMockDevControls();

  if (!import.meta.env.DEV || !dev) return null;

  return (
    <label className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-800 shadow-sm">
      <span className="uppercase tracking-wide text-amber-500">Act as (dev)</span>
      <select
        aria-label="Dev user switcher"
        value={dev.actingUid}
        onChange={(e) => dev.setActingUid(e.target.value)}
        className="rounded bg-transparent text-[11px] font-semibold text-amber-900 focus:outline-none"
      >
        {dev.users.map((u) => (
          <option key={u.uid} value={u.uid}>
            {u.label}
          </option>
        ))}
      </select>
    </label>
  );
}
