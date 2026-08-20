import { useState, type ReactElement } from 'react';
import { Link, Navigate, Route, Routes } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  Td,
  TextField,
  THead,
  Th,
  Tr,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery, useProjectsQuery } from '../../api/projects';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import {
  PO_STATUSES,
  PO_STATUS_LABEL,
  PO_STATUS_TONE,
  usePurchaseOrdersQuery,
  type POFilters,
  type POStatus,
} from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE, rupees } from './salesOrdersFormat';
import { POForm } from './POForm';
import { POUpload } from './POUpload';
import { PODetail } from './PODetail';

const BASE = SALES_ORDERS_BASE;

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/**
 * Purchase Orders — the module's default surface. Owns its own list / new / upload
 * / detail sub-routes (mounted at `sales_orders/*`). Server-side RBAC is the real
 * gate; the create/upload buttons + routes are gated at OPERATE here for honest UX.
 */
export function PurchaseOrdersPage() {
  const perms = usePermissions();
  const canOperate = perms.atLeast('sales_orders', 'OPERATE');
  // Defer the OPERATE route guard until /me resolves so a deep-link isn't bounced
  // before permissions load (mirrors ExpenseModule's Manage-only guard).
  const operateGuard = (node: ReactElement) =>
    perms.loading ? (
      <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
    ) : canOperate ? (
      node
    ) : (
      <Navigate to={BASE} replace />
    );

  return (
    <Routes>
      <Route index element={<Register canOperate={canOperate} />} />
      <Route path="new" element={operateGuard(<POForm />)} />
      <Route path="upload" element={operateGuard(<POUpload />)} />
      <Route path=":id" element={<PODetail />} />
      <Route path="*" element={<Navigate to={BASE} replace />} />
    </Routes>
  );
}

/** The filterable PO register (VIEW). */
function Register({ canOperate }: { canOperate: boolean }) {
  const [q, setQ] = useState('');
  const [clientId, setClientId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [status, setStatus] = useState<POStatus | ''>('');

  const clientsQuery = useClientsQuery();
  // Projects for the filter list follow the chosen client (all statuses, so a PO on a
  // now-inactive project is still filterable); when no client is chosen, list all.
  const projectsQuery = useProjectsQuery({ client_id: clientId });

  const filters: POFilters = { q, client_id: clientId, project_id: projectId, status };
  const debouncedFilters = useDebouncedValue(filters);
  const query = usePurchaseOrdersQuery(debouncedFilters);
  const rows = query.data ?? [];

  return (
    <div>
      <PageHeader
        title="Purchase orders"
        subtitle="Every PO across clients and projects."
        actions={
          canOperate ? (
            <div className="flex items-center gap-2">
              <Link to={`${BASE}/upload`}>
                <Button variant="secondary" size="sm">
                  Upload .xlsx
                </Button>
              </Link>
              <Link to={`${BASE}/new`}>
                <Button size="sm">New PO</Button>
              </Link>
            </div>
          ) : undefined
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="PO number"
          maxLength={80}
        />
        <SelectField
          label="Client"
          value={clientId}
          onChange={(e) => {
            setClientId(e.target.value);
            setProjectId('');
          }}
        >
          <option value="">All clients</option>
          {(clientsQuery.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.code} — {c.name}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Project"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          disabled={!clientId}
          hint={!clientId ? 'Pick a client to filter by project.' : undefined}
        >
          <option value="">All projects</option>
          {(projectsQuery.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.code} — {p.name}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as POStatus | '')}
        >
          <option value="">All statuses</option>
          {PO_STATUSES.map((s) => (
            <option key={s} value={s}>
              {PO_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading purchase orders…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No purchase orders found">No POs match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>PO number</Th>
                <Th>Client</Th>
                <Th>Project</Th>
                <Th>PO date</Th>
                <Th>Status</Th>
                <Th className="text-right">Lines</Th>
                <Th className="text-right">Total sell</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((po) => (
                <Tr key={po.id}>
                  <Td className="font-medium text-slate-900">{po.po_number}</Td>
                  <Td>{po.client_name ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{po.project_code ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{formatDate(po.po_date)}</Td>
                  <Td>
                    <Badge tone={PO_STATUS_TONE[po.status]}>{PO_STATUS_LABEL[po.status]}</Badge>
                  </Td>
                  <Td className="text-right tabular-nums">{po.line_count}</Td>
                  <Td className="text-right tabular-nums">{rupees(po.total_sell_paise)}</Td>
                  <Td>
                    <div className="flex justify-end">
                      <Link
                        to={`${BASE}/${po.id}`}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        View
                      </Link>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          <p className="mt-4 text-sm text-slate-500">Showing {rows.length}</p>
        </>
      )}
    </div>
  );
}
