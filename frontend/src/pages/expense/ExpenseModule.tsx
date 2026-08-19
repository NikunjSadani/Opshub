import { Navigate, Route, Routes } from 'react-router-dom';
import { Tabs, type TabDef } from '../../ui';
import { EXPENSE_BASE } from './expenseFormat';
import { Upload } from './Upload';
import { Register } from './Register';
import { ReviewPanel } from './ReviewPanel';

const BASE = EXPENSE_BASE;

/**
 * Expense / Invoice module shell: a tab bar + nested routes. Server-side RBAC
 * (module access) is the real gate; the tabs are just UX. The invoice detail /
 * review screen is a nested route off the register, not a tab.
 */
export function ExpenseModule() {
  const tabs: TabDef[] = [
    { to: BASE, label: 'Upload', end: true },
    { to: `${BASE}/register`, label: 'Register' },
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<Upload />} />
        <Route path="register" element={<Register />} />
        <Route path="invoices/:id" element={<ReviewPanel />} />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
