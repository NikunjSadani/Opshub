import { Navigate, Route, Routes } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { NewChallan } from './NewChallan';
import { Batches } from './Batches';
import { Register } from './Register';
import { MasterData } from './MasterData';

const BASE = '/m/document_automation';

/**
 * Delivery Challan module shell: a tab bar + nested routes. Master Data is
 * admin-only (config the challan flow depends on). Server-side RBAC is the real
 * gate; hiding the tab is just UX.
 */
export function ChallanModule() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';

  const tabs: TabDef[] = [
    { to: BASE, label: 'New Challan', end: true },
    { to: `${BASE}/batches`, label: 'Batches' },
    { to: `${BASE}/register`, label: 'Register' },
    ...(isAdmin ? [{ to: `${BASE}/master-data`, label: 'Master Data' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<NewChallan />} />
        <Route path="batches" element={<Batches />} />
        <Route path="register" element={<Register />} />
        <Route
          path="master-data/*"
          element={isAdmin ? <MasterData /> : <Navigate to={BASE} replace />}
        />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
