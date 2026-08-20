import { Route, Routes } from 'react-router-dom';
import { Tabs, type TabDef } from '../../ui';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';
import { PurchaseOrdersPage } from './PurchaseOrdersPage';
import { ProductsPage } from './ProductsPage';
import { QuoteSearchPage } from './QuoteSearchPage';

const BASE = SALES_ORDERS_BASE;

/**
 * Sales Orders module shell: a tab bar + nested routes. Server-side RBAC (the
 * `sales_orders` module grant) is the real gate; the tabs are UX. Purchase Orders
 * is the default surface (it owns its own list/new/upload/detail sub-routes);
 * Products and Quote Search are their own segments.
 */
export function SalesOrdersModule() {
  const tabs: TabDef[] = [
    { to: BASE, label: 'Purchase Orders', end: true },
    { to: `${BASE}/products`, label: 'Products' },
    { to: `${BASE}/quote-search`, label: 'Quote Search' },
  ];

  return (
    <div>
      <Tabs tabs={tabs} />
      <Routes>
        <Route path="products/*" element={<ProductsPage />} />
        <Route path="quote-search/*" element={<QuoteSearchPage />} />
        <Route path="*" element={<PurchaseOrdersPage />} />
      </Routes>
    </div>
  );
}
