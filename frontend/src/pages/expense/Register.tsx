import { useState } from 'react';
import { Link } from 'react-router-dom';
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
  useToast,
} from '../../ui';
import { useApi } from '../../api/client';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { useProjectsQuery } from '../../api/projects';
import {
  buildInvoiceCsvQuery,
  usePaymentMethods,
  useInvoicesInfiniteQuery,
  type InvoiceFilters,
  type InvoiceStatus,
} from '../../api/expense';
import {
  EXPENSE_BASE,
  INVOICE_STATUS_LABEL,
  INVOICE_STATUS_TONE,
  errorMessage,
  formatDate,
  formatPaise,
} from './expenseFormat';

export function Register() {
  const toast = useToast();
  const { downloadUrl } = useApi();

  const [q, setQ] = useState('');
  const [status, setStatus] = useState<InvoiceStatus | ''>('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [projectId, setProjectId] = useState('');
  const [paymentMethodId, setPaymentMethodId] = useState('');
  const [downloading, setDownloading] = useState(false);
  const [exportTruncated, setExportTruncated] = useState(false);

  // Filter pickers list ALL projects / payment methods (not active-only) so an
  // invoice tagged to a now-inactive one is still filterable in the register.
  const projectsQuery = useProjectsQuery({});
  const methodsQuery = usePaymentMethods(false);

  // Debounce the filters that feed the query key so each keystroke does not fire
  // its own request; the list refetches ~300ms after typing settles. The
  // immediate filters still feed the CSV export (a deliberate button click).
  const filters: InvoiceFilters = {
    q,
    status,
    date_from: dateFrom,
    date_to: dateTo,
    project_id: projectId,
    payment_method_id: paymentMethodId,
  };
  const debouncedFilters = useDebouncedValue(filters);

  // Changing any filter changes the query key, so paging naturally resets to page 0.
  const query = useInvoicesInfiniteQuery(debouncedFilters);
  const rows = query.data?.pages.flat() ?? [];

  async function onDownloadCsv() {
    setDownloading(true);
    setExportTruncated(false);
    try {
      const result = await downloadUrl(
        `/expense/invoices.csv${buildInvoiceCsvQuery(filters)}`,
        'invoices.csv',
      );
      if (result.truncated) {
        setExportTruncated(true);
        toast.info(
          'The export was capped at the maximum row limit — narrow the filters to get the full set.',
        );
      }
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Register"
        subtitle="All uploaded invoices across every batch."
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
          result set. Add filters (search, status, or a date range) and export again for the full
          data.
        </div>
      )}

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Supplier, GSTIN, invoice no."
          maxLength={80}
        />
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as InvoiceStatus | '')}
        >
          <option value="">All</option>
          <option value="UPLOADED">Uploaded</option>
          <option value="EXTRACTED">Extracted</option>
          <option value="NEEDS_REVIEW">Needs review</option>
          <option value="NEEDS_OCR">Needs OCR</option>
          <option value="CONFIRMED">Confirmed</option>
          <option value="REJECTED">Rejected</option>
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
        <SelectField
          label="Project"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
        >
          <option value="">All projects</option>
          {(projectsQuery.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.code} — {p.name}
              {p.status !== 'ACTIVE' ? ` (${p.status.toLowerCase()})` : ''}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Payment method"
          value={paymentMethodId}
          onChange={(e) => setPaymentMethodId(e.target.value)}
        >
          <option value="">All payment methods</option>
          {(methodsQuery.data ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
              {m.active ? '' : ' (inactive)'}
            </option>
          ))}
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading register…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No invoices found">No invoices match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Supplier</Th>
                <Th>GSTIN</Th>
                <Th>Invoice no.</Th>
                <Th>Date</Th>
                <Th className="text-right">Grand total</Th>
                <Th>Project</Th>
                <Th>Payment method</Th>
                <Th>Status</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((inv) => (
                <Tr key={inv.id}>
                  <Td className="font-medium text-slate-900">{inv.supplier_name ?? '—'}</Td>
                  <Td className="tabular-nums">{inv.supplier_gstin ?? '—'}</Td>
                  <Td>{inv.invoice_number ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{formatDate(inv.invoice_date)}</Td>
                  <Td className="text-right tabular-nums">{formatPaise(inv.grand_total_paise)}</Td>
                  <Td className="whitespace-nowrap">{inv.project_code ?? '—'}</Td>
                  <Td>{inv.payment_method_name ?? '—'}</Td>
                  <Td>
                    <Badge tone={INVOICE_STATUS_TONE[inv.status]}>
                      {INVOICE_STATUS_LABEL[inv.status]}
                    </Badge>
                  </Td>
                  <Td>
                    <div className="flex justify-end">
                      <Link
                        to={`${EXPENSE_BASE}/invoices/${inv.id}`}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        {inv.status === 'NEEDS_REVIEW' ? 'Review' : 'View'}
                      </Link>
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
    </div>
  );
}
