import { Navigate, Route, Routes } from 'react-router-dom';
import { usePermissions } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { ProjectsList } from './ProjectsList';
import { ClientsScreen } from './ClientsScreen';
import { ClientDetail } from './ClientDetail';

const BASE = '/m/projects';

/**
 * Projects module shell: a tab bar + nested routes. The Clients registry is a READ
 * surface — any projects-module user (VIEW+) may open the Clients tab, the list, and
 * a client's detail; the CREATE/EDIT affordances inside are MANAGE-gated in the
 * screens themselves (server-side RBAC is the real gate; the gating here is UX).
 * Showing Clients at VIEW keeps client detail reachable — its only inbound link is
 * the Clients list.
 */
export function ProjectsModule() {
  const perms = usePermissions();
  const canView = perms.canAccessModule('projects');

  const loadingEl = (
    <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
  );

  const tabs: TabDef[] = [
    { to: BASE, label: 'Projects', end: true },
    ...(canView ? [{ to: `${BASE}/clients`, label: 'Clients' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<ProjectsList />} />
        <Route
          path="clients"
          element={
            // A read surface: any projects-module user (VIEW+) may open the Clients
            // list; the in-screen create/edit affordances are MANAGE-gated. Defer the
            // guard until /me resolves — a fresh deep-link must not be bounced before
            // permissions load (mirrors RequirePlatform's loading behaviour).
            perms.loading ? (
              loadingEl
            ) : canView ? (
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
