import { Navigate, Route, Routes } from 'react-router-dom';
import { usePermissions } from '../../auth/AuthProvider';
import { Tabs, type TabDef } from '../../ui';
import { EXPENSE_BASE } from './expenseFormat';
import { Upload } from './Upload';
import { Register } from './Register';
import { Overview } from './Overview';
import { PaymentMethods } from './PaymentMethods';
import { ReviewPanel } from './ReviewPanel';

const BASE = EXPENSE_BASE;

/**
 * Expense / Invoice module shell: a tab bar + nested routes. Server-side RBAC
 * (module access) is the real gate; the tabs are just UX. The invoice detail /
 * review screen is a nested route off the register, not a tab. Payment Methods
 * edits admin-managed reference data, so it needs MANAGE on the module — the tab
 * is hidden and the route guarded for non-managers (mirrors ChallanModule).
 */
export function ExpenseModule() {
  const perms = usePermissions();
  const canManage = perms.atLeast('expense_invoice', 'MANAGE');

  const tabs: TabDef[] = [
    { to: BASE, label: 'Upload', end: true },
    { to: `${BASE}/register`, label: 'Register' },
    { to: `${BASE}/overview`, label: 'Overview' },
    ...(canManage ? [{ to: `${BASE}/payment-methods`, label: 'Payment Methods' }] : []),
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<Upload />} />
        <Route path="register" element={<Register />} />
        <Route path="overview" element={<Overview />} />
        <Route
          path="payment-methods"
          element={
            // Defer the guard until /me resolves — a fresh deep-link must not be
            // bounced before permissions load (mirrors ChallanModule's Master Data).
            perms.loading ? (
              <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
            ) : canManage ? (
              <PaymentMethods />
            ) : (
              <Navigate to={BASE} replace />
            )
          }
        />
        <Route path="invoices/:id" element={<ReviewPanel />} />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
