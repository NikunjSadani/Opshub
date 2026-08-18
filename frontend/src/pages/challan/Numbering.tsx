import { useState } from 'react';
import {
  Badge,
  Button,
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
import {
  useAllocationsQuery,
  useCountersQuery,
  type AllocationFilters,
  type AllocationStatus,
} from '../../api/numbering';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { ALLOCATION_STATUS_TONE } from './challanFormat';

const NUM = new Intl.NumberFormat('en-IN');

/** Compact high-water-mark row: last issued number per series/fy. */
function Counters() {
  const query = useCountersQuery();

  if (query.isPending) return <Loading label="Loading counters…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (query.data.length === 0) {
    return (
      <StatePanel title="No counters yet">
        Numbers appear here once a series has issued its first challan.
      </StatePanel>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {query.data.map((c) => (
        <div key={`${c.series}-${c.fy}`} className="rounded-xl border border-slate-200 bg-white p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            {c.series} · {c.fy}
          </p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">
            {NUM.format(c.last_number)}
          </p>
          <p className="mt-0.5 text-xs text-slate-400">Last issued number</p>
        </div>
      ))}
    </div>
  );
}

export function Numbering() {
  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');
  const [status, setStatus] = useState<AllocationStatus | ''>('');

  // Debounce the text filters (Status is a select, applied immediately) so each
  // keystroke does not fire its own request. Changing any filter changes the
  // query key, so paging naturally resets to page 0.
  const filters: AllocationFilters = {
    series: useDebouncedValue(series),
    fy: useDebouncedValue(fy),
    status,
  };
  const query = useAllocationsQuery(filters);
  const rows = query.data?.pages.flat() ?? [];

  return (
    <div>
      <PageHeader title="Numbering" subtitle="Allocated numbers and per-series high-water marks." />

      <div className="mb-6">
        <Counters />
      </div>

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
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
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as AllocationStatus | '')}
        >
          <option value="">All</option>
          <option value="RESERVED">Reserved</option>
          <option value="ISSUED">Issued</option>
          <option value="VOID">Void</option>
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading allocations…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No allocations found">
          No {status ? `${status.toLowerCase()} ` : ''}numbers match these filters.
        </StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Number</Th>
                <Th>Series</Th>
                <Th>Financial year</Th>
                <Th>Status</Th>
                <Th>Bound to</Th>
                <Th>Void reason</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((a) => (
                <Tr key={a.id}>
                  <Td className="font-medium text-slate-900">{a.formatted}</Td>
                  <Td>{a.series}</Td>
                  <Td className="whitespace-nowrap">{a.fy}</Td>
                  <Td>
                    <Badge tone={ALLOCATION_STATUS_TONE[a.status]}>{a.status}</Badge>
                  </Td>
                  <Td>
                    {a.entity ? (
                      <span>
                        <span className="text-slate-900">{a.entity}</span>
                        {a.entity_id != null && (
                          <span className="text-slate-500"> #{a.entity_id}</span>
                        )}
                      </span>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </Td>
                  <Td className="max-w-xs text-slate-500">{a.void_reason ?? '—'}</Td>
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
