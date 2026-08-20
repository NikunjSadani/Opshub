import { Navigate, Route, Routes } from 'react-router-dom';
import { usePermissions } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { ProjectsList } from './ProjectsList';
import { ClientsScreen } from './ClientsScreen';
import { ClientDetail } from './ClientDetail';

const BASE = '/m/projects';

/**
 * Projects module shell: a tab bar + nested routes. Clients requires MANAGE on
 * the projects module (registering/managing a client is a Manage action).
 * Server-side RBAC is the real gate; hiding the tab is just UX.
 *
 * The Client Master detail (`clients/:id`) is a READ surface — it needs only
 * projects VIEW, so it is guarded on module access (not MANAGE); the edits inside
 * it are MANAGE-gated in the screen itself. It is a nested route, not a tab.
 */
export function ProjectsModule() {
  const perms = usePermissions();
  const canManage = perms.atLeast('projects', 'MANAGE');
  const canView = perms.canAccessModule('projects');

  const loadingEl = (
    <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
  );

  const tabs: TabDef[] = [
    { to: BASE, label: 'Projects', end: true },
    ...(canManage ? [{ to: `${BASE}/clients`, label: 'Clients' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<ProjectsList />} />
        <Route
          path="clients"
          element={
            // Defer the guard until /me resolves — a fresh deep-link to this
            // route must not be bounced before permissions load (mirrors
            // RequirePlatform's loading behaviour).
            perms.loading ? (
              loadingEl
            ) : canManage ? (
              <ClientsScreen />
            ) : (
              <Navigate to={BASE} replace />
            )
          }
        />
        <Route
          path="clients/:id"
          element={
            // A read surface: any projects-module user (VIEW+) may open it; the
            // in-screen edits are MANAGE-gated. Defer until /me resolves.
            perms.loading ? loadingEl : canView ? <ClientDetail /> : <Navigate to={BASE} replace />
          }
        />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
