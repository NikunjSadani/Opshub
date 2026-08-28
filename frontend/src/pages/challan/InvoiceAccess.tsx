import { useMemo, useState } from 'react';
import {
  Badge,
  Card,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  TextField,
  THead,
  Th,
  Tr,
  Td,
} from '../../ui';
import {
  useInvoiceAccessSummary,
  useInvoiceAccessRecent,
  type AccessOutcome,
  type InvoiceAccessFilters,
} from '../../api/invoiceAccess';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';

interface StatTile {
  label: string;
  value: string;
  hint?: string;
}

function StatTiles({ tiles }: { tiles: StatTile[] }) {
  return (
    <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
      {tiles.map((t) => (
        <Card key={t.label} className="p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{t.label}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{t.value}</p>
          {t.hint && <p className="mt-0.5 text-xs text-slate-400">{t.hint}</p>}
        </Card>
      ))}
    </div>
  );
}

const NUM = new Intl.NumberFormat('en-IN');

/** Sentence-case labels for each access outcome, so a state reads the same everywhere. */
const OUTCOME_LABEL: Record<AccessOutcome, string> = {
  VIEWED: 'Viewed',
  WRONG_PIN: 'Wrong PIN',
  NOT_AVAILABLE: 'Not available',
  RATE_LIMITED: 'Rate limited',
  NO_PIN: 'No PIN',
};

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Green = success, amber = benign miss (no invoice yet), red = a failed/blocked attempt. */
const OUTCOME_TONE: Record<AccessOutcome, Tone> = {
  VIEWED: 'green',
  NOT_AVAILABLE: 'amber',
  WRONG_PIN: 'red',
  RATE_LIMITED: 'red',
  NO_PIN: 'red',
};

function OutcomeBadge({ outcome }: { outcome: AccessOutcome }) {
  return <Badge tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABEL[outcome]}</Badge>;
}

/** YYYY-MM-DD `n` days before today, as a local date (no UTC shift). */
function isoDaysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** Localised "when" for a recent-access ISO timestamp (falls back to the raw string). */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

const RECENT_LIMIT = 25;

export function InvoiceAccess() {
  // Default to the last 30 days so the dashboard is populated on first open.
  const [from, setFrom] = useState(() => isoDaysAgo(30));
  const [to, setTo] = useState(() => isoDaysAgo(0));

  // Debounce the date inputs that feed the query key so nudging a date picker
  // does not fire a request per intermediate value; the summary refetches
  // ~300ms after the range settles.
  const filters: InvoiceAccessFilters = {
    from: useDebouncedValue(from),
    to: useDebouncedValue(to),
  };
  const summary = useInvoiceAccessSummary(filters);
  const recent = useInvoiceAccessRecent({ limit: RECENT_LIMIT });

  // Scale the trend bars against the largest single-day total so the tallest bar
  // fills the track; guard the empty case so we never divide by zero.
  const trendMax = useMemo(() => {
    const rows = summary.data?.trend ?? [];
    return rows.reduce((max, r) => Math.max(max, r.views + r.failed), 0);
  }, [summary.data]);

  return (
    <div>
      <PageHeader
        title="Invoice Access"
        subtitle="Tracks who opens the public invoice viewer from a challan QR code — PIN entries, successful views, and failed attempts."
      />

      <div className="mb-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <TextField
          label="From date"
          type="date"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          max={to || undefined}
        />
        <TextField
          label="To date"
          type="date"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          min={from || undefined}
        />
      </div>
      <p className="mb-4 text-xs text-slate-400">
        The public invoice page has no login, so “approx. viewers” is an IP/device-based estimate,
        not an exact count of people.
      </p>

      {summary.isPending ? (
        <Loading label="Loading invoice access…" />
      ) : summary.isError ? (
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      ) : summary.data.total_pin_entries === 0 &&
        summary.data.total_views === 0 &&
        summary.data.by_client.length === 0 ? (
        <StatePanel title="No invoice access yet">
          No one has opened the public invoice viewer in this date range.
        </StatePanel>
      ) : (
        <>
          <StatTiles
            tiles={[
              { label: 'PIN entries', value: NUM.format(summary.data.total_pin_entries) },
              { label: 'Invoices viewed', value: NUM.format(summary.data.total_views) },
              {
                label: 'Failed attempts',
                value: NUM.format(summary.data.total_failed),
                hint: 'wrong PIN / locked / no PIN',
              },
              {
                label: '~Viewers',
                value: NUM.format(summary.data.approx_viewers),
                hint: 'approx, by device',
              },
            ]}
          />

          {summary.data.by_client.length === 0 ? (
            <StatePanel title="No per-client breakdown">
              No client-level access to break down for this range.
            </StatePanel>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Client</Th>
                  <Th className="text-right">PIN entries</Th>
                  <Th className="text-right">Viewed</Th>
                  <Th className="text-right">Failed</Th>
                  <Th className="text-right">~Viewers</Th>
                </Tr>
              </THead>
              <tbody>
                {summary.data.by_client.map((row) => (
                  <Tr key={row.client_id}>
                    <Td>
                      <span className="font-medium text-slate-900">
                        {row.client_name ?? 'Unknown client'}
                      </span>
                      {row.client_code && (
                        <span className="ml-1.5 text-xs text-slate-400">{row.client_code}</span>
                      )}
                    </Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.pin_entries)}</Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.views)}</Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.failed)}</Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.approx_viewers)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}

          {/* Dependency-free trend: a small labelled bar list of views vs failed per day. */}
          <section className="mt-6">
            <h2 className="mb-2 text-sm font-semibold text-slate-900">Daily trend</h2>
            {summary.data.trend.length === 0 ? (
              <StatePanel title="No trend data">
                No day-by-day access to plot for this range.
              </StatePanel>
            ) : (
              <Card className="p-4">
                <div className="mb-3 flex items-center gap-4 text-xs text-slate-500">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-500" /> Views
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="inline-block h-2.5 w-2.5 rounded-sm bg-rose-500" /> Failed
                  </span>
                </div>
                <ul className="space-y-1.5">
                  {summary.data.trend.map((row) => (
                    <li key={row.date} className="flex items-center gap-3">
                      <span className="w-24 shrink-0 text-xs tabular-nums text-slate-500">
                        {row.date}
                      </span>
                      <div
                        className="flex h-4 flex-1 overflow-hidden rounded-sm bg-slate-100"
                        role="img"
                        aria-label={`${row.date}: ${row.views} views, ${row.failed} failed`}
                      >
                        <div
                          className="h-full bg-emerald-500"
                          style={{ width: `${trendMax ? (row.views / trendMax) * 100 : 0}%` }}
                        />
                        <div
                          className="h-full bg-rose-500"
                          style={{ width: `${trendMax ? (row.failed / trendMax) * 100 : 0}%` }}
                        />
                      </div>
                      <span className="w-28 shrink-0 text-right text-xs tabular-nums text-slate-500">
                        {NUM.format(row.views)} / {NUM.format(row.failed)}
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </section>

          {/* Recent access log — a separate query so a slow/empty log never blocks the summary. */}
          <section className="mt-6">
            <h2 className="mb-2 text-sm font-semibold text-slate-900">Recent access</h2>
            {recent.isPending ? (
              <Loading label="Loading recent access…" />
            ) : recent.isError ? (
              <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
            ) : recent.data.length === 0 ? (
              <StatePanel title="No recent access">
                No invoice-viewer access has been recorded yet.
              </StatePanel>
            ) : (
              <Table>
                <THead>
                  <Tr>
                    <Th>Challan number</Th>
                    <Th>Client</Th>
                    <Th>When</Th>
                    <Th>Outcome</Th>
                  </Tr>
                </THead>
                <tbody>
                  {recent.data.map((row, i) => (
                    <Tr key={`${row.challan_number}-${row.accessed_at}-${i}`}>
                      <Td className="font-medium text-slate-900">{row.challan_number}</Td>
                      <Td>{row.client_name ?? <span className="text-slate-400">—</span>}</Td>
                      <Td className="whitespace-nowrap text-slate-600">
                        {formatWhen(row.accessed_at)}
                      </Td>
                      <Td>
                        <OutcomeBadge outcome={row.outcome} />
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </section>
        </>
      )}
    </div>
  );
}
