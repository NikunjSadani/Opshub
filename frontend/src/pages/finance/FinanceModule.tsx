import { useState } from 'react';
import {
  Button,
  Card,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  TextField,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import { useApi } from '../../api/client';
import { useClientsQuery } from '../../api/projects';
import {
  buildPnlQuery,
  useConsolidatedPnl,
  usePnlByProject,
  type PnlFilters,
} from '../../api/finance';

/**
 * Format integer PAISE as an Indian-Rupee string, e.g. 20000000 -> "₹2,00,000.00".
 * Signed: a negative margin renders as "-₹…" (loss). Single paise→₹ seam for the
 * Finance screens; mirrors the shared `rupees` helper.
 */
function rupees(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
}

/** Format a margin percentage, or "—" when it is null (revenue was 0 → undefined ratio). */
function marginPct(pct: number | null | undefined): string {
  if (pct == null) return '—';
  return `${pct.toFixed(1)}%`;
}

/** Red when the value is a loss (< 0), otherwise the normal slate/emphasis colour. */
function signClass(paise: number): string {
  return paise < 0 ? 'text-rose-600' : 'text-slate-900';
}

interface TileProps {
  label: string;
  value: string;
  hint?: string;
  /** Tint the value green/red by sign (used for the margin tile). */
  tone?: 'neutral' | 'signed';
  signValue?: number;
}

function StatTile({ label, value, hint, tone = 'neutral', signValue = 0 }: TileProps) {
  const valueClass =
    tone === 'signed'
      ? signValue < 0
        ? 'text-rose-600'
        : 'text-emerald-600'
      : 'text-slate-900';
  return (
    <Card className="p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${valueClass}`}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-400">{hint}</p>}
    </Card>
  );
}

/**
 * Finance / P&L dashboard. Consolidated tiles (revenue / cost / margin) + a
 * separately-called-out general-overhead bucket, then a per-project P&L table
 * filterable by client + date range, with a CSV export. Money is integer paise
 * on the wire → rendered with the module's ₹ formatter; margin can be negative.
 */
export function FinanceModule() {
  const toast = useToast();
  const { downloadUrl } = useApi();

  const [clientId, setClientId] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [downloading, setDownloading] = useState(false);

  const filters: PnlFilters = { client_id: clientId, date_from: dateFrom, date_to: dateTo };

  const clientsQuery = useClientsQuery();
  const consolidated = useConsolidatedPnl();
  const projects = usePnlByProject(filters);

  async function onDownloadCsv() {
    setDownloading(true);
    try {
      await downloadUrl(`/finance/pnl/projects.csv${buildPnlQuery(filters)}`, 'pnl-projects.csv');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Download failed.');
    } finally {
      setDownloading(false);
    }
  }

  const rows = projects.data ?? [];

  return (
    <div>
      <PageHeader
        title="Finance / P&L"
        subtitle="Revenue, cost and margin by project, with consolidated totals."
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void onDownloadCsv()}
            loading={downloading}
          >
            Download CSV
          </Button>
        }
      />

      {/* --- Consolidated totals + general-overhead callout --- */}
      {consolidated.isPending ? (
        <Loading label="Loading consolidated P&L…" />
      ) : consolidated.isError ? (
        <ErrorState error={consolidated.error} onRetry={() => void consolidated.refetch()} />
      ) : (
        <>
          <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatTile
              label="Total revenue"
              value={rupees(consolidated.data.totals.revenue_paise)}
              hint="Across all projects"
            />
            <StatTile
              label="Total cost"
              value={rupees(consolidated.data.totals.cost_paise)}
              hint="Including general overhead"
            />
            <StatTile
              label="Total margin"
              value={rupees(consolidated.data.totals.margin_paise)}
              hint="Revenue − cost"
              tone="signed"
              signValue={consolidated.data.totals.margin_paise}
            />
          </div>

          <div className="mb-6 rounded-xl border border-amber-200 bg-amber-50 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-amber-800">
                  General / overhead
                </p>
                <p className="mt-0.5 text-sm text-amber-700">
                  Overhead not attributed to a specific project — shown separately, and
                  already included in the total cost above.
                </p>
              </div>
              <div className="grid grid-cols-3 gap-x-6 text-right tabular-nums">
                <div>
                  <p className="text-xs text-amber-700">Revenue</p>
                  <p className="text-sm font-semibold text-slate-900">
                    {rupees(consolidated.data.general_bucket.revenue_paise)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-amber-700">Cost</p>
                  <p className="text-sm font-semibold text-slate-900">
                    {rupees(consolidated.data.general_bucket.cost_paise)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-amber-700">Margin</p>
                  <p
                    className={`text-sm font-semibold ${signClass(
                      consolidated.data.general_bucket.margin_paise,
                    )}`}
                  >
                    {rupees(consolidated.data.general_bucket.margin_paise)}
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* --- Unattributed callout: PO-less confirmed invoices with no project. Shown
               only when it actually carries money, so it stays out of the way otherwise.
               Distinct from the GEN overhead bucket; already folded into the totals. --- */}
          {consolidated.data.unattributed &&
            (consolidated.data.unattributed.revenue_paise !== 0 ||
              consolidated.data.unattributed.cost_paise !== 0) && (
              <div className="mb-6 rounded-xl border border-slate-300 bg-slate-50 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wide text-slate-600">
                      Unattributed
                    </p>
                    <p className="mt-0.5 text-sm text-slate-500">
                      Confirmed invoices with no PO and no project — assign a project on the
                      invoice to move this into that project&apos;s P&amp;L. Already included in
                      the totals above.
                    </p>
                  </div>
                  <div className="grid grid-cols-3 gap-x-6 text-right tabular-nums">
                    <div>
                      <p className="text-xs text-slate-500">Revenue</p>
                      <p className="text-sm font-semibold text-slate-900">
                        {rupees(consolidated.data.unattributed.revenue_paise)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-500">Cost</p>
                      <p className="text-sm font-semibold text-slate-900">
                        {rupees(consolidated.data.unattributed.cost_paise)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-500">Margin</p>
                      <p
                        className={`text-sm font-semibold ${signClass(
                          consolidated.data.unattributed.margin_paise,
                        )}`}
                      >
                        {rupees(consolidated.data.unattributed.margin_paise)}
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            )}
        </>
      )}

      {/* --- Per-project P&L table --- */}
      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SelectField
          label="Client"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
        >
          <option value="">All clients</option>
          {(clientsQuery.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
              {c.active ? '' : ' (inactive)'}
            </option>
          ))}
        </SelectField>
        <TextField
          label="From date"
          type="date"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          max={dateTo || undefined}
        />
        <TextField
          label="To date"
          type="date"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          min={dateFrom || undefined}
        />
      </div>

      {projects.isPending ? (
        <Loading label="Loading project P&L…" />
      ) : projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No project P&L found">
          No projects match these filters.
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Client</Th>
              <Th>Project</Th>
              <Th className="text-right">Revenue</Th>
              <Th className="text-right">Cost</Th>
              <Th className="text-right">Margin</Th>
              <Th className="text-right">Margin %</Th>
            </Tr>
          </THead>
          <tbody>
            {rows.map((r) => (
              <Tr key={r.project_id}>
                <Td className="whitespace-nowrap text-slate-700">{r.client_name}</Td>
                <Td className="font-medium text-slate-900">
                  {r.project_code} — {r.project_name}
                </Td>
                <Td className="text-right tabular-nums">{rupees(r.revenue_paise)}</Td>
                <Td className="text-right tabular-nums">{rupees(r.cost_paise)}</Td>
                <Td className={`text-right tabular-nums font-medium ${signClass(r.margin_paise)}`}>
                  {rupees(r.margin_paise)}
                </Td>
                <Td
                  className={`text-right tabular-nums ${
                    r.margin_pct != null && r.margin_pct < 0 ? 'text-rose-600' : 'text-slate-700'
                  }`}
                >
                  {marginPct(r.margin_pct)}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}
