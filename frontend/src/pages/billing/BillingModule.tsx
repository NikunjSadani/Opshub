import { Route, Routes } from 'react-router-dom';
import { Tabs, type TabDef } from '../../ui';
import { BILLING_BASE } from './billingFormat';
import { InvoicesPage } from './InvoicesPage';
import { CreditNotesPage } from './CreditNotesPage';
import { ReceivablesPage } from './ReceivablesPage';
import { AdvancesPage } from './AdvancesPage';

const BASE = BILLING_BASE;

/**
 * Billing & AR module shell: tabs + nested routes. Server-side RBAC (the `billing`
 * module grant) is the real gate. Invoices owns its own upload/register/review/match
 * sub-routes; Receivables (AR tracker + payments) and Advances are their own segments.
 */
export function BillingModule() {
  const tabs: TabDef[] = [
    { to: BASE, label: 'Invoices', end: true },
    { to: `${BASE}/credit-notes`, label: 'Credit Notes' },
    { to: `${BASE}/receivables`, label: 'Receivables' },
    { to: `${BASE}/advances`, label: 'Advances' },
  ];
  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route path="credit-notes/*" element={<CreditNotesPage />} />
        <Route path="receivables/*" element={<ReceivablesPage />} />
        <Route path="advances/*" element={<AdvancesPage />} />
        <Route path="*" element={<InvoicesPage />} />
      </Routes>
    </div>
  );
}
