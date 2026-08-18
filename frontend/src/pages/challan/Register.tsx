import { useEffect, useState } from 'react';
import {
  Badge,
  Button,
  ConfirmDialog,
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
import { useAuth } from '../../auth/AuthProvider';
import { useApi } from '../../api/client';
import {
  buildChallanCsvQuery,
  useChallansInfiniteQuery,
  useVoidChallan,
  type ChallanFilters,
  type ChallanOut,
  type ChallanStatus,
} from '../../api/challan';
import { CHALLAN_STATUS_TONE, errorMessage, formatPaise } from './challanFormat';

function formatDate(iso: string): string {
  // challan_date is a plain YYYY-MM-DD; format the parts directly so a UTC parse
  // can't shift it a day in timezones behind UTC.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

export function Register() {
  const toast = useToast();
  const { user } = useAuth();
  const { download, downloadUrl } = useApi();
  const isAdmin = user?.role === 'ADMIN';

  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');
  const [status, setStatus] = useState<ChallanStatus | ''>('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [downloading, setDownloading] = useState(false);
  const [exportTruncated, setExportTruncated] = useState(false);

  const filters: ChallanFilters = { series, fy, status, date_from: dateFrom, date_to: dateTo };
  // Debounce the filters that feed the query key so each keystroke in Series /
  // Financial year / dates does not fire its own request; the list refetches
  // ~300ms after typing settles. The immediate `filters` is still used for the
  // CSV export (a deliberate button click, not a keystroke).
  const [debouncedFilters, setDebouncedFilters] = useState<ChallanFilters>(filters);
  useEffect(() => {
    const t = window.setTimeout(
      () => setDebouncedFilters({ series, fy, status, date_from: dateFrom, date_to: dateTo }),
      300,
    );
    return () => window.clearTimeout(t);
  }, [series, fy, status, dateFrom, dateTo]);

  // Changing any filter changes the query key, so paging naturally resets to page 0.
  const query = useChallansInfiniteQuery(debouncedFilters);
  const rows = query.data?.pages.flat() ?? [];

  const voidMutation = useVoidChallan();
  const [voidTarget, setVoidTarget] = useState<ChallanOut | null>(null);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | undefined>();

  function openVoid(c: ChallanOut) {
    setVoidTarget(c);
    setReason('');
    setReasonError(undefined);
  }

  function closeVoid() {
    setVoidTarget(null);
    setReason('');
    setReasonError(undefined);
    voidMutation.reset();
  }

  function confirmVoid() {
    if (!voidTarget) return;
    if (!reason.trim()) {
      setReasonError('A reason is required to void.');
      return;
    }
    voidMutation.mutate(
      { challanId: voidTarget.id, reason: reason.trim() },
      {
        onSuccess: (updated) => {
          toast.success(`Challan ${updated.number} voided.`);
          closeVoid();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  async function onDownloadCsv() {
    setDownloading(true);
    setExportTruncated(false);
    try {
      const result = await downloadUrl(
        `/challan/challans.csv${buildChallanCsvQuery(filters)}`,
        'challans.csv',
      );
      if (result.truncated) {
        setExportTruncated(true);
        toast.info('The export was capped at the maximum row limit — narrow the filters to get the full set.');
      }
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  async function onDownloadPdf(c: ChallanOut) {
    if (c.pdf_file_id == null) return;
    try {
      await download(c.pdf_file_id, `${c.number.replace(/\//g, '-')}.pdf`);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  return (
    <div>
      <PageHeader
        title="Register"
        subtitle="Issued challans across all batches."
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

      {exportTruncated && (
        <div
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          The last CSV export was capped at the maximum row limit, so it is not the complete
          result set. Add filters (series, financial year, or a date range) and export again for
          the full data.
        </div>
      )}

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-5">
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
          onChange={(e) => setStatus(e.target.value as ChallanStatus | '')}
        >
          <option value="">All</option>
          <option value="ISSUED">Issued</option>
          <option value="VOID">Void</option>
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

      {query.isPending ? (
        <Loading label="Loading register…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No challans found">
          No {status ? `${status.toLowerCase()} ` : ''}challans match these filters.
        </StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Number</Th>
                <Th>Date</Th>
                <Th>Project ID</Th>
                <Th>Consignee</Th>
                <Th>Ship-to state</Th>
                <Th>E-way</Th>
                <Th className="text-right">Total</Th>
                <Th>Status</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((c) => (
                <Tr key={c.id}>
                  <Td className="font-medium text-slate-900">{c.number}</Td>
                  <Td className="whitespace-nowrap">{formatDate(c.challan_date)}</Td>
                  <Td>
                    {c.project_code ? (
                      <span className="font-medium text-slate-900">{c.project_code}</span>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </Td>
                  <Td>
                    <span className="text-slate-600">{c.consignee_name}</span>
                  </Td>
                  <Td>{c.ship_to_state}</Td>
                  <Td>
                    <Badge tone={c.eway_required ? 'amber' : 'slate'}>
                      {c.eway_required ? 'Yes' : 'No'}
                    </Badge>
                  </Td>
                  <Td className="text-right tabular-nums">{formatPaise(c.total_paise)}</Td>
                  <Td>
                    <Badge tone={CHALLAN_STATUS_TONE[c.status]}>{c.status}</Badge>
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-1.5">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onDownloadPdf(c)}
                        disabled={c.pdf_file_id == null}
                      >
                        PDF
                      </Button>
                      {isAdmin && c.status === 'ISSUED' && (
                        <Button variant="danger" size="sm" onClick={() => openVoid(c)}>
                          Void
                        </Button>
                      )}
                    </div>
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

      <ConfirmDialog
        open={voidTarget != null}
        title="Void challan"
        confirmLabel="Void challan"
        danger
        loading={voidMutation.isPending}
        onConfirm={confirmVoid}
        onCancel={closeVoid}
        message={
          <div>
            <p className="mb-3">
              This voids challan <span className="font-semibold">{voidTarget?.number}</span> and its
              bound number. This cannot be undone.
            </p>
            <TextField
              label="Reason"
              required
              value={reason}
              onChange={(e) => {
                setReason(e.target.value);
                if (reasonError) setReasonError(undefined);
              }}
              error={reasonError}
              placeholder="Why is this being voided?"
              maxLength={300}
            />
          </div>
        }
      />
    </div>
  );
}
