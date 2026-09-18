import { useMemo, useState } from 'react';
import { Link, Navigate, Route, Routes } from 'react-router-dom';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  PageHeader,
  SearchableSelect,
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
import { usePermissions } from '../../auth/AuthProvider';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { useClientsQuery } from '../../api/projects';
import { usePurchaseOrdersQuery } from '../../api/purchaseOrders';
import {
  useBillingInvoicesQuery,
  useDeleteInvoice,
  type BillingInvoiceFilters,
  type SalesInvoiceStatus,
} from '../../api/billingInvoices';
import { BILLING_BASE } from './billingFormat';
import { InvoiceUpload } from './InvoiceUpload';
import { InvoiceReview } from './InvoiceReview';
import {
  INVOICE_STATUS_LABEL,
  INVOICE_STATUS_TONE,
  errorMessage,
  formatDate,
  money,
} from './billingInvoiceFormat';

/** Every stored invoice status, in display order (used by the status filter). */
const STATUS_OPTIONS: readonly SalesInvoiceStatus[] = [
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
  const toast = useToast();
  const perms = usePermissions();
  // Delete-and-re-upload is a MANAGE affordance (mirrors the invoice detail screen).
  const canManage = perms.atLeast('billing', 'MANAGE');
  const deleteInvoice = useDeleteInvoice();

  const [q, setQ] = useState('');
  const [status, setStatus] = useState<SalesInvoiceStatus | ''>('');
  const [clientId, setClientId] = useState('');
  const [poId, setPoId] = useState('');
  // A single dialog drives every row's delete: `deleteTarget` is the id of the invoice
  // pending deletion (null = closed). One dialog, not one per row. The row id is a string
  // on the wire (backend `InvoiceOut`), so track it as such.
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const clientsQuery = useClientsQuery();
  // POs for the filter are scoped to the chosen client (or all when none chosen).
  const posQuery = usePurchaseOrdersQuery(clientId ? { client_id: clientId } : {});

  // Lookup maps for the client + PO columns (the register rows carry only ids).
  const clientById = useMemo(
    () => new Map((clientsQuery.data ?? []).map((c) => [String(c.id), c])),
    [clientsQuery.data],
  );
  const poById = useMemo(
    () => new Map((posQuery.data ?? []).map((p) => [String(p.id), p])),
    [posQuery.data],
  );

  const filters: BillingInvoiceFilters = { q, status, client_id: clientId, po_id: poId };
  const debouncedFilters = useDebouncedValue(filters);

  const query = useBillingInvoicesQuery(debouncedFilters);
  const rows = query.data?.pages.flat() ?? [];

  function onClientChange(value: string) {
    setClientId(value);
    // A PO belongs to one client — clear a stale PO filter when the client changes.
    setPoId('');
  }

  function onConfirmDelete() {
    if (deleteTarget == null) return;
    deleteInvoice.mutate(deleteTarget, {
      onSuccess: () => {
        setDeleteTarget(null);
        // The mutation invalidates the register, so the row disappears on its own.
        toast.success('Invoice deleted.');
      },
      // Surface the backend's honest refusal (e.g. 409 "invoice has receivable records and
      // cannot be deleted") instead of failing silently.
      onError: (err) => {
        setDeleteTarget(null);
        toast.error(errorMessage(err));
      },
    });
  }

  // The row being deleted (matched by stable id) drives a row-specific confirm — the one
  // safety net against a wrong-row click on a long list. A CONFIRMED invoice also gets an
  // explicit warning, because deleting it reverses its billed-qty contribution to the PO.
  const deleteRow = deleteTarget != null ? rows.find((r) => r.id === deleteTarget) : undefined;
  const deleteLabel = deleteRow?.invoice_number ?? (deleteRow ? `#${deleteRow.id}` : '');
  const deleteTitle = deleteRow ? `Delete invoice ${deleteLabel}?` : 'Delete this invoice?';
  const deleteMessage =
    'This permanently deletes the invoice and its source PDF. This cannot be undone. ' +
    'An invoice that already has payments, credit notes or advances against it cannot be deleted.' +
    (deleteRow?.status === 'CONFIRMED'
      ? ' Note: this invoice is CONFIRMED — deleting it reverses its billed quantity on the linked PO.'
      : '');

  return (
    <div>
      <PageHeader
        title="Client invoices"
        subtitle="Uploaded client GST invoices, their PO-match state, and confirmation status."
        actions={
          <Link
            to={`${BILLING_BASE}/upload`}
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-brand-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-brand-700"
          >
            Upload invoices
          </Link>
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buyer, GSTIN, invoice no."
          maxLength={80}
        />
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as SalesInvoiceStatus | '')}
        >
          <option value="">All</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {INVOICE_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
        <SearchableSelect
          label="Client"
          value={clientId}
          onChange={onClientChange}
          noneLabel="All clients"
          placeholder="Search a client…"
          options={(clientsQuery.data ?? []).map((c) => ({
            value: String(c.id),
            label: `${c.code} — ${c.name}`,
          }))}
        />
        <SearchableSelect
          label="Purchase order"
          value={poId}
          onChange={setPoId}
          disabled={posQuery.isPending}
          noneLabel="All purchase orders"
          placeholder="Search a purchase order…"
          options={(posQuery.data ?? []).map((p) => ({
            value: String(p.id),
            label: `${p.po_number}${p.project_code ? ` — ${p.project_code}` : ''}`,
          }))}
        />
      </div>

      {query.isPending ? (
        <Loading label="Loading invoices…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No invoices found">No invoices match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Invoice no.</Th>
                <Th>Client</Th>
                <Th>PO</Th>
                <Th>Date</Th>
                <Th className="text-right">Grand total</Th>
                <Th>Status</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((inv) => {
                const client = clientById.get(String(inv.client_id));
                const po = inv.po_id != null ? poById.get(String(inv.po_id)) : undefined;
                return (
                  <Tr key={inv.id}>
                    <Td className="font-medium text-slate-900">{inv.invoice_number ?? '—'}</Td>
                    <Td className="whitespace-nowrap">
                      {client ? `${client.code} — ${client.name}` : `#${inv.client_id}`}
                    </Td>
                    <Td className="whitespace-nowrap">
                      {inv.po_id == null ? '—' : (po?.po_number ?? `#${inv.po_id}`)}
                    </Td>
                    <Td className="whitespace-nowrap">{formatDate(inv.invoice_date)}</Td>
                    <Td className="text-right tabular-nums">{money(inv.grand_total_paise)}</Td>
                    <Td>
                      <Badge tone={INVOICE_STATUS_TONE[inv.status]}>
                        {INVOICE_STATUS_LABEL[inv.status]}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-3">
                        <Link
                          to={`${BILLING_BASE}/${inv.id}`}
                          className="text-sm font-medium text-brand-600 hover:text-brand-700"
                        >
                          {inv.status === 'CONFIRMED' || inv.status === 'CANCELLED' ? 'View' : 'Review'}
                        </Link>
                        {canManage && (
                          <Button
                            variant="danger"
                            size="sm"
                            aria-label={`Delete invoice ${inv.invoice_number ?? inv.id}`}
                            onClick={() => setDeleteTarget(inv.id)}
                          >
                            Delete
                          </Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                );
              })}
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

      {/* One dialog for the whole list, driven by the pending-delete id. Mirrors the
          invoice detail screen's delete (permanent, blocked once receivables exist). */}
      <ConfirmDialog
        open={deleteTarget != null}
        title={deleteTitle}
        confirmLabel="Delete invoice"
        danger
        loading={deleteInvoice.isPending}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={onConfirmDelete}
        message={deleteMessage}
      />
    </div>
  );
}

/**
 * Client-invoice capture segment of the Billing & AR module. Owns its own routes
 * (relative to `/m/billing`): the register (index), the bulk upload, and the
 * per-invoice detail + match-review screen. Server-side RBAC (the `billing` module)
 * is the real gate; these screens set honest expectations on top of it.
 */
export function InvoicesPage() {
  return (
    <Routes>
      <Route index element={<Register />} />
      <Route path="upload" element={<InvoiceUpload />} />
      <Route path=":id" element={<InvoiceReview />} />
      <Route path="*" element={<Navigate to={BILLING_BASE} replace />} />
    </Routes>
  );
}
