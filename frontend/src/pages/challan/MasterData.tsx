import { Navigate, Route, Routes } from 'react-router-dom';
import { Tabs, type TabDef } from '../../ui';
import { ConsignorScreen } from './masterdata/ConsignorScreen';
import { ConsigneeScreen } from './masterdata/ConsigneeScreen';
import { HsnScreen } from './masterdata/HsnScreen';
import { SeriesScreen } from './masterdata/SeriesScreen';

const BASE = '/m/document_automation/master-data';

/**
 * Master Data admin sub-module: a sub-tab bar over the four reference registries
 * the challan flow depends on (Consignor, Consignee, HSN, Series). Mounted by
 * ChallanModule under `master-data/*` (admin-only); this component owns the
 * nested routing below it.
 */
export function MasterData() {
  const tabs: TabDef[] = [
    { to: BASE, label: 'Consignor', end: true },
    { to: `${BASE}/consignee`, label: 'Consignee' },
    { to: `${BASE}/hsn`, label: 'HSN Codes' },
    { to: `${BASE}/series`, label: 'Series' },
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<ConsignorScreen />} />
        <Route path="consignee" element={<ConsigneeScreen />} />
        <Route path="hsn" element={<HsnScreen />} />
        <Route path="series" element={<SeriesScreen />} />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
