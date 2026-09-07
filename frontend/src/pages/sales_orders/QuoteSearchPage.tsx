import { useMemo, useState } from 'react';
import {
  Badge,
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
} from '../../ui';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery } from '../../api/projects';
import { rupees } from './salesOrdersFormat';
import {
  usePriceTrend,
  useQuoteSearch,
  type QuoteFilters,
  type QuoteRow,
} from '../../api/quoteSearch';

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** A readable product label: name, with brand / model as a muted suffix. */
function productLabel(row: Pick<QuoteRow, 'product_name' | 'brand' | 'model_number'>): string {
  const extra = [row.brand, row.model_number].filter(Boolean).join(' · ');
  return extra ? `${row.product_name} (${extra})` : row.product_name;
}

/** Render a margin percentage with a tone that flags thin / negative margins. */
function MarginBadge({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="text-slate-400">—</span>;
  const tone = pct < 0 ? 'red' : pct < 10 ? 'amber' : 'green';
  return <Badge tone={tone}>{pct.toFixed(1)}%</Badge>;
}

/**
 * Quote / price-book search — the pitch-speed lookup tool. A keyword box + facets
 * (category, client, date range, budget in ₹) over priced PO lines, showing
 * cost / client price and the freight/packaging/handling adders. Clicking a row
 * opens that product's price history so inflation over time is visible.
 *
 * VIEW-gated: the `sales_orders` module route already guards access. Only IAM admins
 * see the ACTUAL sell + true margin; the API masks both (null) for everyone else, so
 * the extra columns are rendered only when `hasPlatform('iam')`. Everyone sees the
 * client-quoted price, and the budget facet filters on that client price.
 */
export function QuoteSearchPage() {
  // Only admins (platform IAM) may see the "actual" sell + margin — the API masks them
  // (null) for everyone else. Matches the backend has_platform(IAM) gate.
  const isAdmin = usePermissions().hasPlatform('iam');
  const [q, setQ] = useState('');
  const [category, setCategory] = useState('');
  const [clientId, setClientId] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [budgetMin, setBudgetMin] = useState('');
  const [budgetMax, setBudgetMax] = useState('');
  const [trendFor, setTrendFor] = useState<{ id: string; label: string } | null>(null);

  const clientsQuery = useClientsQuery();

  // Debounce the filter set so each keystroke does not fire its own request; the
  // list refetches ~300ms after typing settles. Memoise on the primitive values so
  // the object identity is stable — a fresh literal each render would make the
  // debounced value churn forever (a perpetual 300ms re-render loop).
  const filters: QuoteFilters = useMemo(
    () => ({
      q,
      category,
      client_id: clientId,
      date_from: dateFrom,
      date_to: dateTo,
      budget_min: budgetMin,
      budget_max: budgetMax,
    }),
    [q, category, clientId, dateFrom, dateTo, budgetMin, budgetMax],
  );
  const debounced = useDebouncedValue(filters);
  const search = useQuoteSearch(debounced);
  const rows = search.data ?? [];

  const trend = usePriceTrend(trendFor?.id ?? null);

  return (
    <div>
      <PageHeader
        title="Quote Search"
        subtitle="Search past purchase-order prices to quote fast — cost, sell, margin, and price history over time."
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Keyword"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Product, brand, model, PO no."
          maxLength={80}
        />
        <TextField
          label="Category"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          placeholder="e.g. Apparel"
          maxLength={80}
        />
        <SelectField
          label="Client"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          error={clientsQuery.isError ? "Couldn't load clients." : undefined}
        >
          <option value="">
            {clientsQuery.isError ? 'Failed to load clients' : 'All clients'}
          </option>
          {(clientsQuery.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </SelectField>
        <div className="grid grid-cols-2 gap-2">
          <TextField
            label="Client price min (₹)"
            type="number"
            inputMode="decimal"
            min={0}
            value={budgetMin}
            onChange={(e) => setBudgetMin(e.target.value)}
            placeholder="0"
          />
          <TextField
            label="Client price max (₹)"
            type="number"
            inputMode="decimal"
            min={0}
            value={budgetMax}
            onChange={(e) => setBudgetMax(e.target.value)}
            placeholder="Any"
          />
        </div>
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

      {search.isPending ? (
        <Loading label="Searching price book…" />
      ) : search.isError ? (
        <ErrorState error={search.error} onRetry={() => void search.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No matches">
          No priced purchase-order lines match these filters. Try a broader keyword or clear a
          facet.
        </StatePanel>
      ) : (
        <>
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <Tr>
                  <Th>Product</Th>
                  <Th>Client</Th>
                  <Th>PO no.</Th>
                  <Th>Date</Th>
                  <Th className="text-right">Qty</Th>
                  <Th className="text-right">Cost</Th>
                  <Th className="text-right">Client price</Th>
                  {isAdmin && <Th className="text-right">Actual sell</Th>}
                  {isAdmin && <Th className="text-right">Margin</Th>}
                  <Th className="text-right">Freight</Th>
                  {isAdmin && <Th className="text-right">Actual frt</Th>}
                  <Th className="text-right">Packaging</Th>
                  <Th className="text-right">Handling</Th>
                  <Th className="text-right">Trend</Th>
                </Tr>
              </THead>
              <tbody>
                {rows.map((row) => (
                  <Tr key={row.po_line_item_id}>
                    <Td className="font-medium text-slate-900">
                      <div>{row.product_name}</div>
                      {(row.brand || row.model_number) && (
                        <div className="text-xs text-slate-400">
                          {[row.brand, row.model_number].filter(Boolean).join(' · ')}
                        </div>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap">{row.client_name}</Td>
                    <Td className="whitespace-nowrap">{row.po_number}</Td>
                    <Td className="whitespace-nowrap font-medium text-slate-900">
                      {formatDate(row.po_date)}
                    </Td>
                    <Td className="whitespace-nowrap text-right tabular-nums">
                      {row.ordered_qty} {row.uom}
                    </Td>
                    <Td className="text-right tabular-nums">{rupees(row.cost_price_paise)}</Td>
                    <Td className="text-right tabular-nums">
                      {rupees(row.client_sell_price_paise)}
                    </Td>
                    {isAdmin && (
                      <Td className="text-right tabular-nums">
                        {row.sell_price_paise == null ? '—' : rupees(row.sell_price_paise)}
                      </Td>
                    )}
                    {isAdmin && (
                      <Td className="text-right">
                        <MarginBadge pct={row.margin_pct} />
                      </Td>
                    )}
                    {/* VISIBLE client freight for everyone; the ACTUAL freight is admin-only. */}
                    <Td className="text-right tabular-nums text-slate-500">
                      {rupees(row.client_freight_paise)}
                    </Td>
                    {isAdmin && (
                      <Td className="text-right tabular-nums text-slate-500">
                        {row.freight_paise == null ? '—' : rupees(row.freight_paise)}
                      </Td>
                    )}
                    <Td className="text-right tabular-nums text-slate-500">
                      {rupees(row.packaging_paise)}
                    </Td>
                    <Td className="text-right tabular-nums text-slate-500">
                      {rupees(row.handling_paise)}
                    </Td>
                    <Td className="text-right">
                      <button
                        type="button"
                        onClick={() =>
                          setTrendFor({ id: row.product_id, label: productLabel(row) })
                        }
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        Trend
                      </button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
          <p className="mt-4 text-sm text-slate-500">Showing {rows.length}</p>
        </>
      )}

      {trendFor && (
        <Card className="mt-5 p-4">
          <div className="mb-3 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">Price history</h2>
              <p className="mt-0.5 text-sm text-slate-500">{trendFor.label}</p>
            </div>
            <Button variant="ghost" size="sm" onClick={() => setTrendFor(null)}>
              Close
            </Button>
          </div>

          {trend.isPending ? (
            <Loading label="Loading price history…" />
          ) : trend.isError ? (
            <ErrorState error={trend.error} onRetry={() => void trend.refetch()} />
          ) : (trend.data ?? []).length === 0 ? (
            <StatePanel title="No price history">
              This product has no other priced purchase-order lines yet.
            </StatePanel>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Date</Th>
                  <Th>PO no.</Th>
                  <Th>Client</Th>
                  <Th className="text-right">Qty</Th>
                  <Th className="text-right">Cost</Th>
                  <Th className="text-right">Client price</Th>
                  {isAdmin && <Th className="text-right">Actual sell</Th>}
                </Tr>
              </THead>
              <tbody>
                {(trend.data ?? []).map((p, i) => (
                  <Tr key={`${p.po_number}-${i}`}>
                    <Td className="whitespace-nowrap font-medium text-slate-900">
                      {formatDate(p.po_date)}
                    </Td>
                    <Td className="whitespace-nowrap">{p.po_number}</Td>
                    <Td className="whitespace-nowrap">{p.client_name}</Td>
                    <Td className="text-right tabular-nums">{p.ordered_qty}</Td>
                    <Td className="text-right tabular-nums">{rupees(p.cost_price_paise)}</Td>
                    <Td className="text-right tabular-nums">
                      {rupees(p.client_sell_price_paise)}
                    </Td>
                    {isAdmin && (
                      <Td className="text-right tabular-nums">
                        {p.sell_price_paise == null ? '—' : rupees(p.sell_price_paise)}
                      </Td>
                    )}
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      )}
    </div>
  );
}
