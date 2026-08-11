import { useState } from 'react';
import {
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
import { useChallanSummaryQuery, type ChallanSummaryFilters } from '../../api/challan';
import { formatPaise } from './challanFormat';

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

export function Overview() {
  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');

  const filters: ChallanSummaryFilters = { series, fy };
  const query = useChallanSummaryQuery(filters);

  return (
    <div>
      <PageHeader title="Overview" subtitle="Issued / void counts and value across the register." />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <TextField
          label="Series"
          value={series}
          onChange={(e) => setSeries(e.target.value)}
          placeholder="e.g. L"
          maxLength={8}
        />
        <TextField
          label="Financial year"
          value={fy}
          onChange={(e) => setFy(e.target.value)}
          placeholder="e.g. 26-27"
          maxLength={7}
        />
      </div>

      {query.isPending ? (
        <Loading label="Loading overview…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.issued_count === 0 &&
        query.data.void_count === 0 &&
        query.data.by_series.length === 0 ? (
        <StatePanel title="No challans found">No challans match these filters.</StatePanel>
      ) : (
        <>
          <StatTiles
            tiles={[
              { label: 'Issued', value: NUM.format(query.data.issued_count) },
              { label: 'Void', value: NUM.format(query.data.void_count) },
              {
                label: 'E-way',
                value: NUM.format(query.data.eway_count),
                hint: 'Issued needing an e-way bill',
              },
              {
                label: 'Total value',
                value: formatPaise(query.data.total_value_paise),
                hint: `${NUM.format(query.data.valued_count)} valued`,
              },
            ]}
          />

          {query.data.by_series.length === 0 ? (
            <StatePanel title="No series breakdown">
              No issued or void challans to break down.
            </StatePanel>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Series</Th>
                  <Th>Financial year</Th>
                  <Th className="text-right">Issued</Th>
                  <Th className="text-right">Void</Th>
                  <Th className="text-right">Total value</Th>
                </Tr>
              </THead>
              <tbody>
                {query.data.by_series.map((row) => (
                  <Tr key={`${row.series}-${row.fy}`}>
                    <Td className="font-medium text-slate-900">{row.series}</Td>
                    <Td className="whitespace-nowrap">{row.fy}</Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.issued)}</Td>
                    <Td className="text-right tabular-nums">{NUM.format(row.void)}</Td>
                    <Td className="text-right tabular-nums">{formatPaise(row.total_value_paise)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </>
      )}
    </div>
  );
}
