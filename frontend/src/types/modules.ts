/**
 * A navigable module, as served by `GET /api/v1/modules`. The backend is the
 * authority on which modules a given user may access (per-user module grants,
 * see docs/DESIGN.md §3) — the FE just renders what it receives.
 */
export interface ModuleDescriptor {
  /** Stable identifier, also used as the route segment (e.g. "delivery-challan"). */
  key: string;
  /** Human label for nav + tiles. */
  title: string;
  /** Sidebar grouping label (e.g. "Modules", "Platform"). */
  nav_group: string;
  /** When true, the tile/nav item renders disabled with a "Coming Soon" badge. */
  coming_soon: boolean;
}
