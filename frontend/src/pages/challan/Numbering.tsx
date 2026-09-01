import { useState } from 'react';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
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
import {
  useAllocationsQuery,
  useCountersQuery,
  useSeedSeries,
  type AllocationFilters,
  type AllocationStatus,
} from '../../api/numbering';
import { usePermissions } from '../../auth/AuthProvider';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { ALLOCATION_STATUS_TONE } from './challanFormat';

const SERIES_RE = /^[A-Za-z0-9]{1,8}$/;
const FY_RE = /^\d{2}-\d{2}$/;

/** Mirror the backend `_clean_fy` rule: the two 2-digit years must be consecutive
 * (handles the 99→00 wrap). Caller checks FY_RE first. */
function fyConsecutive(fy: string): boolean {
  const [a, b] = fy.split('-').map(Number);
  return (((b - a) % 100) + 100) % 100 === 1;
}

/**
 * Admin action: configure the starting number for a (series, FY) so challan
 * generation can reserve numbers. Conditionally MOUNTED by the parent (fresh
 * state per open). Gated to MANAGE at the call site; backend re-checks and
 * guards against lowering below an already-issued number.
 */
function SeedSeriesModal({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const seed = useSeedSeries();
  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');
  const [lastNumber, setLastNumber] = useState('0');
  const [errors, setErrors] = useState<{ series?: string; fy?: string; last?: string }>({});

  function submit() {
    const s = series.trim().toUpperCase();
    const f = fy.trim();
    const next: typeof errors = {};
    if (!SERIES_RE.test(s)) next.series = '1–8 letters or digits';
    if (f) {
      if (!FY_RE.test(f)) next.fy = 'Format YY-YY, e.g. 26-27';
      else if (!fyConsecutive(f)) next.fy = 'Years must be consecutive, e.g. 26-27';
    }
    const n = Number(lastNumber);
    if (lastNumber.trim() === '' || !Number.isInteger(n) || n < 0 || n > 999_999) {
      next.last = 'A whole number 0–999999';
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    seed.mutate(
      { series: s, fy: f || undefined, last_number: n },
      {
        onSuccess: (c) => {
          toast.success(`${c.series} · ${c.fy} configured — the next challan will be ${c.last_number + 1}.`);
          onClose();
        },
        onError: (e) => toast.error(e.message || 'Could not set the starting number.'),
      },
    );
  }

  return (
    <Modal
      open
      title="Set starting number"
      onClose={onClose}
      busy={seed.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={seed.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={seed.isPending}>
            Set starting number
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-slate-600">
          Tell the system where a series&rsquo; challan numbers begin, per financial year. The next
          challan will be the number you enter <strong>plus one</strong>. This can never lower a
          number that has already been issued.
        </p>
        <TextField
          label="Series"
          required
          maxLength={8}
          className="font-mono uppercase"
          value={series}
          error={errors.series}
          hint="e.g. M1 (must match the series you enter at Number &amp; generate)"
          onChange={(e) => setSeries(e.target.value.toUpperCase())}
        />
        <TextField
          label="Financial year"
          value={fy}
          error={errors.fy}
          maxLength={7}
          placeholder="26-27"
          hint="e.g. 26-27. Leave blank for the current financial year."
          onChange={(e) => setFy(e.target.value)}
        />
        <TextField
          label="Last issued number"
          required
          inputMode="numeric"
          value={lastNumber}
          error={errors.last}
          hint="The next challan will be this + 1. For a brand-new series enter 0 (the first challan becomes 1)."
          onChange={(e) => setLastNumber(e.target.value.replace(/[^0-9]/g, ''))}
        />
        {(() => {
          // Show the resulting first number BEFORE committing — the seed can only ever
          // move the sequence forward (never down), so a too-high entry permanently skips
          // statutory numbers. Surfacing the next number here lets the user catch a typo.
          const n = Number(lastNumber);
          if (lastNumber.trim() === '' || !Number.isInteger(n) || n < 0 || n > 999_999) return null;
          return (
            <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-700">
              The next challan will be{' '}
              <strong className="tabular-nums text-slate-900">{n + 1}</strong>. This cannot be
              undone downward — a number, once passed, is never reissued.
            </p>
          );
        })()}
      </div>
    </Modal>
  );
}

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
  const perms = usePermissions();
  // Seeding a starting number configures the statutory sequence — MANAGE-gated
  // (mirrors series.seed on the backend). Reads stay open to any module viewer.
  const canSeed = perms.atLeast('document_automation', 'MANAGE');
  const [seedOpen, setSeedOpen] = useState(false);

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
      <PageHeader
        title="Numbering"
        subtitle="Allocated numbers and per-series high-water marks."
        actions={
          canSeed ? (
            <Button size="sm" onClick={() => setSeedOpen(true)}>
              Set starting number
            </Button>
          ) : undefined
        }
      />

      {seedOpen && <SeedSeriesModal onClose={() => setSeedOpen(false)} />}

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
