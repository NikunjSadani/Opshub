import { Navigate, Route, Routes } from 'react-router-dom';
import { usePermissions } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { Overview } from './Overview';
import { NewChallan } from './NewChallan';
import { Batches } from './Batches';
import { Register } from './Register';
import { DownloadChallans } from './DownloadChallans';
import { Numbering } from './Numbering';
import { MasterData } from './MasterData';

const BASE = '/m/document_automation';

/**
 * Delivery Challan module shell: a tab bar + nested routes. Master Data edits
 * the shared config the challan flow depends on, so it needs MANAGE on the
 * document_automation module. Server-side RBAC is the real gate; hiding the tab
 * is just UX.
 */
export function ChallanModule() {
  const perms = usePermissions();
  const canManage = perms.atLeast('document_automation', 'MANAGE');

  const tabs: TabDef[] = [
    { to: BASE, label: 'Overview', end: true },
    { to: `${BASE}/new`, label: 'New Challan' },
    { to: `${BASE}/batches`, label: 'Batches' },
    { to: `${BASE}/register`, label: 'Register' },
    { to: `${BASE}/download`, label: 'Download' },
    { to: `${BASE}/numbering`, label: 'Numbering' },
    ...(canManage ? [{ to: `${BASE}/master-data`, label: 'Master Data' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<Overview />} />
        <Route path="new" element={<NewChallan />} />
        <Route path="batches" element={<Batches />} />
        <Route path="register" element={<Register />} />
        <Route path="download" element={<DownloadChallans />} />
        <Route path="numbering" element={<Numbering />} />
        <Route
          path="master-data/*"
          element={canManage ? <MasterData /> : <Navigate to={BASE} replace />}
        />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
