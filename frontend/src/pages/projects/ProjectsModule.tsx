import { Navigate, Route, Routes } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { ProjectsList } from './ProjectsList';
import { ClientsScreen } from './ClientsScreen';

const BASE = '/m/projects';

/**
 * Projects module shell: a tab bar + nested routes. Clients is admin-only
 * (registering a client is an ADMIN action). Server-side RBAC is the real gate;
 * hiding the tab is just UX.
 */
export function ProjectsModule() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';

  const tabs: TabDef[] = [
    { to: BASE, label: 'Projects', end: true },
    ...(isAdmin ? [{ to: `${BASE}/clients`, label: 'Clients' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<ProjectsList />} />
        <Route
          path="clients"
          element={isAdmin ? <ClientsScreen /> : <Navigate to={BASE} replace />}
        />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
