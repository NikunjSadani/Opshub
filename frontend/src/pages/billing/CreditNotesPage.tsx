import { useMemo, useState } from 'react';
import { Link, Navigate, Route, Routes } from 'react-router-dom';
import {
  Badge,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
} from '../../ui';
import { useBillingInvoicesQuery } from '../../api/billingInvoices';
import {
  useCreditNotesQuery,
  type CreditNoteFilters,
  type CreditNoteStatus,
} from '../../api/billingCreditNotes';
import { BILLING_BASE } from './billingFormat';
import { formatDate, money } from './billingInvoiceFormat';
import { CN_STATUS_LABEL, CN_STATUS_TONE } from './creditNoteFormat';
import { CreditNoteUpload } from './CreditNoteUpload';
import { CreditNoteReview } from './CreditNoteReview';

/** Every stored credit-note status, in display order (used by the status filter). */
const STATUS_OPTIONS: readonly CreditNoteStatus[] = [
  'UPLOADED',
  'EXTRACTED',
  'NEEDS_REVIEW',
  'NEEDS_OCR',
  'NEEDS_MATCH',
  'MATCHED',
  'CONFIRMED',
  'REJECTED',
  'CANCELLED',
];

function Register() {
  const [status, setStatus] = useState<CreditNoteStatus | ''>('');
  const [invoiceId, setInvoiceId] = useState('');

  // The invoice filter lists CONFIRMED invoices (the only ones a CN can credit).
  const invoicesQuery = useBillingInvoicesQuery({ status: 'CONFIRMED' });
  const invoices = useMemo(() => invoicesQuery.data?.pages.flat() ?? [], [invoicesQuery.data]);

  const filters: CreditNoteFilters = { status, invoice_id: invoiceId };
  const query = useCreditNotesQuery(filters);
  const rows = query.data ?? [];

  return (
    <div>
      <PageHeader
        title="Credit notes"
        subtitle="Client credit notes issued against confirmed invoices, their PO-match state, and confirmation status."
        actions={
          <Link
            to={`${BILLING_BASE}/credit-notes/upload`}
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-brand-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-brand-700"
          >
            Upload credit notes
          </Link>
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as CreditNoteStatus | '')}
        >
          <option value="">All</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {CN_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Credited invoice"
          value={invoiceId}
          onChange={(e) => setInvoiceId(e.target.value)}
          disabled={invoicesQuery.isPending}
        >
          <option value="">All invoices</option>
          {invoices.map((inv) => (
            <option key={inv.id} value={inv.id}>
              {inv.invoice_number ?? `Invoice #${inv.id}`}
            </option>
          ))}
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading credit notes…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No credit notes found">No credit notes match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Credit note no.</Th>
                <Th>Against invoice</Th>
                <Th>Date</Th>
                <Th className="text-right">Credit total</Th>
                <Th>Status</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((cn) => (
                <Tr key={cn.id}>
                  <Td className="font-medium text-slate-900">{cn.cn_number ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{cn.invoice_number ?? `#${cn.invoice_id}`}</Td>
                  <Td className="whitespace-nowrap">{formatDate(cn.cn_date)}</Td>
                  <Td className="text-right tabular-nums">{money(cn.grand_total_paise)}</Td>
                  <Td>
                    <Badge tone={CN_STATUS_TONE[cn.status]}>{CN_STATUS_LABEL[cn.status]}</Badge>
                  </Td>
                  <Td>
                    <div className="flex justify-end">
                      <Link
                        to={`${BILLING_BASE}/credit-notes/${cn.id}`}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        {cn.status === 'CONFIRMED' || cn.status === 'CANCELLED' ? 'View' : 'Review'}
                      </Link>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>

          <div className="mt-4">
            <p className="text-sm text-slate-500">Showing {rows.length}</p>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Client credit-note capture segment of the Billing & AR module. Owns its own routes
 * (relative to `/m/billing/credit-notes`): the register (index), the upload, and the
 * per-credit-note detail + match-review screen. Server-side RBAC (the `billing` module)
 * is the real gate; these screens set honest expectations on top of it.
 */
export function CreditNotesPage() {
  return (
    <Routes>
      <Route index element={<Register />} />
      <Route path="upload" element={<CreditNoteUpload />} />
      <Route path=":id" element={<CreditNoteReview />} />
      <Route path="*" element={<Navigate to={`${BILLING_BASE}/credit-notes`} replace />} />
    </Routes>
  );
}
