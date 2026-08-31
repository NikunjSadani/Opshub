import { useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  Tabs,
  TextField,
  THead,
  Th,
  Tr,
  Td,
  useToast,
  type TabDef,
} from '../../ui';
import { useApi } from '../../api/client';
import {
  buildAuditEventsCsvQuery,
  buildAuditLoginsCsvQuery,
  useAuditEvents,
  useAuditIntegrity,
  useAuditLogins,
  type AuditEvent,
  type AuditEventFilters,
  type AuditLogin,
  type AuditLoginFilters,
  type AuditPrincipal,
} from '../../api/audit';

const BASE = '/admin/audit';

/** Localised "when" for an ISO timestamp; falls back to the raw string, em dash for null. */
function formatWhen(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Human message from a thrown value — never "[object Object]". */
function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : 'Something went wrong.';
}

/** Render a resolved principal (name + email), or a muted placeholder when null. */
function Principal({ who }: { who: AuditPrincipal | null }) {
  if (!who) return <span className="text-slate-400">Unknown</span>;
  const name = who.name?.trim();
  return (
    <div className="leading-tight">
      {name && <div className="font-medium text-slate-900">{name}</div>}
      <div className={name ? 'text-xs text-slate-500' : 'font-medium text-slate-900'}>
        {who.email || '—'}
      </div>
    </div>
  );
}

/** Render a free-form `detail` (string, object, or null) compactly and safely. */
function formatDetail(detail: unknown): string {
  if (detail == null) return '—';
  if (typeof detail === 'string') return detail;
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

/** Green "intact" / red "broken" tamper-evidence badge for the activity trail. */
function IntegrityBadge() {
  const { data, isPending, isError } = useAuditIntegrity();
  if (isPending) {
    return <span className="text-xs text-slate-400">Checking integrity…</span>;
  }
  if (isError || !data) {
    return <span className="text-xs text-slate-400">Integrity unknown</span>;
  }
  if (data.intact) {
    return <Badge tone="green">Audit trail intact ✓</Badge>;
  }
  return (
    <Badge tone="red">
      Audit trail BROKEN{data.broken_at_id != null ? ` (at #${data.broken_at_id})` : ''}
    </Badge>
  );
}

// ---------------------------------------------------------------- Activity tab

function ActivityTab() {
  const toast = useToast();
  const { downloadUrl } = useApi();

  const [actor, setActor] = useState('');
  const [action, setAction] = useState('');
  const [entity, setEntity] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [downloading, setDownloading] = useState(false);

  const filters: AuditEventFilters = {
    actor,
    action,
    entity,
    date_from: dateFrom,
    date_to: dateTo,
  };
  // Debounce the filters that feed the query key so each keystroke does not fire
  // its own request; the list refetches ~300ms after typing settles. The
  // immediate `filters` still drives the CSV export (a deliberate click).
  const [debounced, setDebounced] = useState<AuditEventFilters>(filters);
  useEffect(() => {
    const t = window.setTimeout(
      () => setDebounced({ actor, action, entity, date_from: dateFrom, date_to: dateTo }),
      300,
    );
    return () => window.clearTimeout(t);
  }, [actor, action, entity, dateFrom, dateTo]);

  const query = useAuditEvents(debounced);
  const rows: AuditEvent[] = query.data?.pages.flatMap((p) => p.items) ?? [];

  async function onExport() {
    setDownloading(true);
    try {
      await downloadUrl(`/admin/audit/events${buildAuditEventsCsvQuery(filters)}`, 'audit-events.csv');
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <IntegrityBadge />
        <Button variant="secondary" size="sm" onClick={() => void onExport()} loading={downloading}>
          Export CSV
        </Button>
      </div>

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <TextField label="User" value={actor} onChange={(e) => setActor(e.target.value)} placeholder="email or name" />
        <TextField label="Action" value={action} onChange={(e) => setAction(e.target.value)} placeholder="e.g. update" />
        <TextField label="Entity" value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="e.g. user" />
        <TextField label="From date" type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} max={dateTo || undefined} />
        <TextField label="To date" type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} min={dateFrom || undefined} />
      </div>

      {query.isPending ? (
        <Loading label="Loading activity…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No audit events found">No activity matches these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>When</Th>
                <Th>User</Th>
                <Th>Action</Th>
                <Th>Entity</Th>
                <Th>Details</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((e) => (
                <Tr key={e.id}>
                  <Td className="whitespace-nowrap text-slate-600">{formatWhen(e.created_at)}</Td>
                  <Td>
                    <Principal who={e.actor} />
                  </Td>
                  <Td>
                    <span className="font-medium text-slate-900">{e.action}</span>
                  </Td>
                  <Td className="whitespace-nowrap">
                    <span className="text-slate-700">{e.entity}</span>
                    {e.entity_id != null && <span className="text-slate-400"> #{e.entity_id}</span>}
                  </Td>
                  <Td className="max-w-md text-slate-500">
                    <span className="block truncate" title={formatDetail(e.detail)}>
                      {formatDetail(e.detail)}
                    </span>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>

          <div className="mt-4 flex items-center justify-between gap-3">
            <p className="text-sm text-slate-500">Showing {rows.length}</p>
            {query.hasNextPage && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void query.fetchNextPage()}
                loading={query.isFetchingNextPage}
              >
                Load more
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ Logins tab

function LoginsTab() {
  const toast = useToast();
  const { downloadUrl } = useApi();

  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [downloading, setDownloading] = useState(false);

  const filters: AuditLoginFilters = { date_from: dateFrom, date_to: dateTo };
  const [debounced, setDebounced] = useState<AuditLoginFilters>(filters);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced({ date_from: dateFrom, date_to: dateTo }), 300);
    return () => window.clearTimeout(t);
  }, [dateFrom, dateTo]);

  const query = useAuditLogins(debounced);
  const rows: AuditLogin[] = query.data?.pages.flatMap((p) => p.items) ?? [];

  async function onExport() {
    setDownloading(true);
    try {
      await downloadUrl(`/admin/audit/logins${buildAuditLoginsCsvQuery(filters)}`, 'audit-logins.csv');
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField label="From date" type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} max={dateTo || undefined} />
          <TextField label="To date" type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} min={dateFrom || undefined} />
        </div>
        <Button variant="secondary" size="sm" onClick={() => void onExport()} loading={downloading}>
          Export CSV
        </Button>
      </div>

      {query.isPending ? (
        <Loading label="Loading logins…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No login events found">No sign-ins match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>When</Th>
                <Th>User</Th>
                <Th>IP</Th>
                <Th>Device / UA</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((l) => (
                <Tr key={l.id}>
                  <Td className="whitespace-nowrap text-slate-600">{formatWhen(l.occurred_at)}</Td>
                  <Td>
                    <Principal who={l.user} />
                  </Td>
                  <Td className="whitespace-nowrap tabular-nums text-slate-700">{l.ip || '—'}</Td>
                  <Td className="max-w-md text-slate-500">
                    <span className="block truncate" title={l.user_agent ?? undefined}>
                      {l.user_agent || '—'}
                    </span>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>

          <div className="mt-4 flex items-center justify-between gap-3">
            <p className="text-sm text-slate-500">Showing {rows.length}</p>
            {query.hasNextPage && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void query.fetchNextPage()}
                loading={query.isFetchingNextPage}
              >
                Load more
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Audit & Access (ADMIN, `iam`-gated at the route). A tab bar + nested routes,
 * mirroring the challan module shell. Server-side RBAC is the real gate; the
 * route guard just avoids a dead-end for someone without `iam`.
 */
export function AuditModule() {
  const tabs: TabDef[] = [
    { to: BASE, label: 'Activity', end: true },
    { to: `${BASE}/logins`, label: 'Logins' },
  ];

  return (
    <div>
      <PageHeader
        title="Audit & Access"
        subtitle="Who did what, and who signed in — with a tamper-evidence check."
      />
      <Tabs tabs={tabs} />
      <Routes>
        <Route index element={<ActivityTab />} />
        <Route path="logins" element={<LoginsTab />} />
        <Route path="*" element={<Navigate to={BASE} replace />} />
      </Routes>
    </div>
  );
}
