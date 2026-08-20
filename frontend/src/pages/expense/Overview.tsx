import type { ReactNode } from 'react';
import {
  Card,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
} from '../../ui';
import { useExpenseSummary } from '../../api/expense';
import { formatPaise } from './expenseFormat';

const NUM = new Intl.NumberFormat('en-IN');

interface StatTile {
  label: string;
  value: string;
  hint?: string;
}

function StatTiles({ tiles }: { tiles: StatTile[] }) {
  return (
    <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2">
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

/** A row's share of the largest total, as a 0..100 width for the inline bar. */
function barWidth(value: number, max: number): string {
  if (max <= 0) return '0%';
  return `${Math.max(2, Math.round((value / max) * 100))}%`;
}

/** A small "spend by X" breakdown table with an inline bar per row. */
function BreakdownTable({
  title,
  emptyLabel,
  headers,
  rows,
}: {
  title: string;
  emptyLabel: string;
  headers: [string, string];
  rows: { key: string; label: ReactNode; total_paise: number; count: number }[];
}) {
  const max = rows.reduce((m, r) => Math.max(m, r.total_paise), 0);
  return (
    <div>
      <h2 className="mb-2 text-sm font-semibold text-slate-900">{title}</h2>
      {rows.length === 0 ? (
        <StatePanel title={emptyLabel}>Confirmed invoices will appear here.</StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>{headers[0]}</Th>
              <Th className="text-right">Invoices</Th>
              <Th className="text-right">{headers[1]}</Th>
              <Th className="w-40">{''}</Th>
            </Tr>
          </THead>
          <tbody>
            {rows.map((r) => (
              <Tr key={r.key}>
                <Td className="font-medium text-slate-900">{r.label}</Td>
                <Td className="text-right tabular-nums">{NUM.format(r.count)}</Td>
                <Td className="text-right tabular-nums">{formatPaise(r.total_paise)}</Td>
                <Td>
                  <div className="h-2 w-full rounded-full bg-slate-100" aria-hidden="true">
                    <div
                      className="h-2 rounded-full bg-brand-500"
                      style={{ width: barWidth(r.total_paise, max) }}
                    />
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

/**
 * Expense Overview: confirmed-spend dashboard. Totals + two breakdowns (by
 * project, by payment method) from GET /expense/summary. Money is integer paise
 * → rendered with the module's ₹ formatter.
 */
export function Overview() {
  const query = useExpenseSummary();

  return (
    <div>
      <PageHeader
        title="Overview"
        subtitle="Confirmed expense spend, broken down by project and payment method."
      />

      {query.isPending ? (
        <Loading label="Loading overview…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <StatTiles
            tiles={[
              {
                label: 'Confirmed spend',
                value: formatPaise(query.data.total_confirmed_paise),
                hint: 'Total across confirmed invoices',
              },
              {
                label: 'Confirmed invoices',
                value: NUM.format(query.data.invoice_count),
              },
            ]}
          />

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <BreakdownTable
              title="Spend by project"
              emptyLabel="No project spend yet"
              headers={['Project', 'Spend']}
              rows={query.data.by_project.map((r) => ({
                key: `p-${r.project_id ?? 'none'}`,
                label: r.project_code
                  ? r.project_name
                    ? `${r.project_code} — ${r.project_name}`
                    : r.project_code
                  : 'Unallocated',
                total_paise: r.total_paise,
                count: r.count,
              }))}
            />
            <BreakdownTable
              title="Spend by payment method"
              emptyLabel="No payment-method spend yet"
              headers={['Payment method', 'Spend']}
              rows={query.data.by_payment_method.map((r) => ({
                key: `m-${r.payment_method_id ?? 'none'}`,
                label: r.name ?? 'Unallocated',
                total_paise: r.total_paise,
                count: r.count,
              }))}
            />
          </div>
        </>
      )}
    </div>
  );
}
