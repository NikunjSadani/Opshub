import { Navigate, Route, Routes } from 'react-router-dom';
import { usePermissions } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { ProjectsList } from './ProjectsList';
import { ClientsScreen } from './ClientsScreen';

const BASE = '/m/projects';

/**
 * Projects module shell: a tab bar + nested routes. Clients requires MANAGE on
 * the projects module (registering/managing a client is a Manage action).
 * Server-side RBAC is the real gate; hiding the tab is just UX.
 */
export function ProjectsModule() {
  const perms = usePermissions();
  const canManage = perms.atLeast('projects', 'MANAGE');

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
          element={canManage ? <ClientsScreen /> : <Navigate to={BASE} replace />}
        />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
