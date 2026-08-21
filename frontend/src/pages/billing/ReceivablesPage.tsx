import { useMemo, useState } from 'react';
import { Link, Navigate, Route, Routes, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  Td,
  TextField,
  THead,
  Th,
  Tr,
  useToast,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery, type Client } from '../../api/projects';
import { ApiError } from '../../api/client';
import { BILLING_BASE, rupees } from './billingFormat';
import {
  AR_STATUSES,
  AR_STATUS_LABEL,
  AR_STATUS_TONE,
  rupeesToPaise,
  useAdvancesQuery,
  useAdvanceSuggestion,
  useApplyAdvance,
  useArQuery,
  useInvoiceArQuery,
  useRecordPayment,
  useUnapplyAdvance,
  type ArFilters,
  type ArStatus,
} from '../../api/billingAr';

const BASE = `${BILLING_BASE}/receivables`;

/** Today as YYYY-MM-DD (computed at call time — never a hardcoded date). */
function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Integer paise → a plain rupee string suitable for a text input (e.g. 10050 → "100.50"). */
function paiseToInput(paise: number): string {
  return (paise / 100).toFixed(2);
}

/** A stable client_id → "CODE — Name" map, for the AR row's display name (AR rows carry
 *  only client_id — the name is joined from the clients list). */
function useClientLabels(): (id: number) => string {
  const clientsQuery = useClientsQuery();
  const byId = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of clientsQuery.data ?? []) m.set(c.id, `${c.code} — ${c.name}`);
    return m;
  }, [clientsQuery.data]);
  return (id: number) => byId.get(String(id)) ?? `Client #${id}`;
}

/**
 * Receivables — the Billing module's AR tracker. Owns its own list / detail sub-routes
 * (mounted at `receivables/*`). Server-side RBAC (`billing` module + discrete
 * payment/advance actions) is the real gate; the record/apply controls are gated at
 * OPERATE here for honest UX.
 */
export function ReceivablesPage() {
  return (
    <Routes>
      <Route index element={<ArRegister />} />
      <Route path=":id" element={<ArDetail />} />
      <Route path="*" element={<Navigate to={BASE} replace />} />
    </Routes>
  );
}

// ============================================================ AR register

function ArRegister() {
  const [clientId, setClientId] = useState('');
  const [status, setStatus] = useState<ArStatus | ''>('');
  const [overdue, setOverdue] = useState(false);

  const clientsQuery = useClientsQuery();
  const labelFor = useClientLabels();

  const filters: ArFilters = useMemo(
    () => ({ client_id: clientId, status, overdue }),
    [clientId, status, overdue],
  );
  const query = useArQuery(filters);
  const rows = query.data ?? [];

  return (
    <div>
      <PageHeader
        title="Receivables"
        subtitle="Outstanding, status and aging for every confirmed invoice."
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SelectField
          label="Client"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          error={clientsQuery.isError ? "Couldn't load clients." : undefined}
        >
          <option value="">{clientsQuery.isError ? 'Failed to load clients' : 'All clients'}</option>
          {(clientsQuery.data ?? []).map((c: Client) => (
            <option key={c.id} value={c.id}>
              {c.code} — {c.name}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as ArStatus | '')}
        >
          <option value="">All statuses</option>
          {AR_STATUSES.map((s) => (
            <option key={s} value={s}>
              {AR_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
        <label className="flex items-end pb-2">
          <span className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={overdue}
              onChange={(e) => setOverdue(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500/40"
            />
            Overdue only
          </span>
        </label>
      </div>

      {query.isPending ? (
        <Loading label="Loading receivables…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No receivables found">No invoices match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Invoice</Th>
                <Th>Client</Th>
                <Th>Due</Th>
                <Th className="text-right">Total</Th>
                <Th className="text-right">Outstanding</Th>
                <Th>Status</Th>
                <Th>Aging</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((r) => (
                <Tr key={r.invoice_id}>
                  <Td className="font-medium text-slate-900">{r.invoice_number}</Td>
                  <Td>{labelFor(r.client_id)}</Td>
                  <Td className="whitespace-nowrap">
                    <span className={r.overdue ? 'font-medium text-rose-600' : undefined}>
                      {formatDate(r.due_date)}
                    </span>
                  </Td>
                  <Td className="text-right tabular-nums">{rupees(r.grand_total_paise)}</Td>
                  <Td className="text-right tabular-nums font-medium text-slate-900">
                    {rupees(r.outstanding_paise)}
                  </Td>
                  <Td>
                    <div className="flex items-center gap-1.5">
                      <Badge tone={AR_STATUS_TONE[r.status]}>{AR_STATUS_LABEL[r.status]}</Badge>
                      {r.overdue && <Badge tone="red">Overdue</Badge>}
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap">{r.aging_bucket ? `${r.aging_bucket} days` : '—'}</Td>
                  <Td>
                    <div className="flex justify-end">
                      <Link
                        to={`${BASE}/${r.invoice_id}`}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        View
                      </Link>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          <p className="mt-4 text-sm text-slate-500">Showing {rows.length}</p>
        </>
      )}
    </div>
  );
}

// ============================================================ AR detail

function ArDetail() {
  const { id } = useParams<{ id: string }>();
  const invoiceId = id ? Number(id) : null;
  const perms = usePermissions();
  const canOperate = perms.atLeast('billing', 'OPERATE');
  const labelFor = useClientLabels();

  const query = useInvoiceArQuery(invoiceId);
  const detail = query.data;

  const [payOpen, setPayOpen] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);

  const toast = useToast();
  const unapply = useUnapplyAdvance();
  const [unapplyId, setUnapplyId] = useState<number | null>(null);

  // Client's advances — used to label applied-advance rows by reference/date.
  const advancesQuery = useAdvancesQuery(detail ? String(detail.client_id) : '');
  const advanceLabel = useMemo(() => {
    const m = new Map<number, string>();
    for (const a of advancesQuery.data ?? []) {
      const bits = [a.reference, formatDate(a.received_on)].filter(Boolean);
      m.set(a.advance_id, bits.length ? bits.join(' · ') : `Advance #${a.advance_id}`);
    }
    return m;
  }, [advancesQuery.data]);

  function doUnapply(applicationId: number) {
    setUnapplyId(applicationId);
    unapply.mutate(applicationId, {
      onSuccess: () => {
        toast.success('Advance application reversed.');
        setUnapplyId(null);
      },
      onError: (e) => {
        toast.error(e instanceof ApiError ? e.message : 'Could not reverse the application.');
        setUnapplyId(null);
      },
    });
  }

  return (
    <div>
      <div className="mb-4">
        <Link to={BASE} className="text-sm font-medium text-brand-600 hover:text-brand-700">
          ← Back to receivables
        </Link>
      </div>

      {query.isPending ? (
        <Loading label="Loading invoice…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : !detail ? (
        <StatePanel title="Invoice not found" />
      ) : (
        <>
          <PageHeader
            title={`Invoice ${detail.invoice_number}`}
            subtitle={labelFor(detail.client_id)}
            actions={
              canOperate ? (
                <div className="flex items-center gap-2">
                  <Button variant="secondary" size="sm" onClick={() => setApplyOpen(true)}>
                    Apply advance
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => setPayOpen(true)}
                    disabled={detail.outstanding_paise <= 0}
                  >
                    Record payment
                  </Button>
                </div>
              ) : undefined
            }
          />

          {/* Money summary */}
          <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="Grand total" value={rupees(detail.grand_total_paise)} />
            <Stat label="Credited" value={rupees(detail.credited_paise)} />
            <Stat label="Paid" value={rupees(detail.paid_paise)} />
            <Stat label="Advances applied" value={rupees(detail.applied_paise)} />
            <Stat label="Outstanding" value={rupees(detail.outstanding_paise)} strong />
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <p className="text-xs font-medium text-slate-500">Status</p>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <Badge tone={AR_STATUS_TONE[detail.status]}>{AR_STATUS_LABEL[detail.status]}</Badge>
                {detail.overdue && <Badge tone="red">Overdue</Badge>}
              </div>
              <p className="mt-1 text-xs text-slate-400">Due {formatDate(detail.due_date)}</p>
            </div>
          </div>

          {/* Payments */}
          <Card className="mb-5 p-4">
            <h2 className="mb-3 text-sm font-semibold text-slate-900">Payments</h2>
            {detail.payments.length === 0 ? (
              <p className="text-sm text-slate-500">No payments recorded yet.</p>
            ) : (
              <Table>
                <THead>
                  <Tr>
                    <Th>Received</Th>
                    <Th>Mode</Th>
                    <Th>Reference</Th>
                    <Th>Note</Th>
                    <Th className="text-right">Amount</Th>
                  </Tr>
                </THead>
                <tbody>
                  {detail.payments.map((p) => (
                    <Tr key={p.id}>
                      <Td className="whitespace-nowrap">{formatDate(p.received_on)}</Td>
                      <Td>{p.mode ?? '—'}</Td>
                      <Td>{p.reference ?? '—'}</Td>
                      <Td>{p.note ?? '—'}</Td>
                      <Td className="text-right tabular-nums">{rupees(p.amount_paise)}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          {/* Applied advances */}
          <Card className="p-4">
            <h2 className="mb-3 text-sm font-semibold text-slate-900">Applied advances</h2>
            {detail.applied_advances.length === 0 ? (
              <p className="text-sm text-slate-500">No advances applied to this invoice.</p>
            ) : (
              <Table>
                <THead>
                  <Tr>
                    <Th>Advance</Th>
                    <Th>Applied on</Th>
                    <Th className="text-right">Amount</Th>
                    <Th className="text-right">Actions</Th>
                  </Tr>
                </THead>
                <tbody>
                  {detail.applied_advances.map((a) => (
                    <Tr key={a.id}>
                      <Td>{advanceLabel.get(a.advance_id) ?? `Advance #${a.advance_id}`}</Td>
                      <Td className="whitespace-nowrap">{formatDate(a.created_at)}</Td>
                      <Td className="text-right tabular-nums">{rupees(a.amount_paise)}</Td>
                      <Td>
                        <div className="flex justify-end">
                          {canOperate ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              loading={unapply.isPending && unapplyId === a.id}
                              disabled={unapply.isPending}
                              onClick={() => doUnapply(a.id)}
                            >
                              Un-apply
                            </Button>
                          ) : (
                            <span className="text-xs text-slate-400">—</span>
                          )}
                        </div>
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          {canOperate && detail && (
            <RecordPaymentModal
              open={payOpen}
              onClose={() => setPayOpen(false)}
              invoiceId={detail.invoice_id}
              outstandingPaise={detail.outstanding_paise}
              onDone={() => setPayOpen(false)}
            />
          )}
          {canOperate && detail && (
            <ApplyAdvanceModal
              open={applyOpen}
              onClose={() => setApplyOpen(false)}
              invoiceId={detail.invoice_id}
              clientId={detail.client_id}
              outstandingPaise={detail.outstanding_paise}
              onDone={() => setApplyOpen(false)}
            />
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs font-medium text-slate-500">{label}</p>
      <p className={`mt-1 tabular-nums ${strong ? 'text-base font-semibold text-slate-900' : 'text-sm text-slate-700'}`}>
        {value}
      </p>
    </div>
  );
}

// ============================================================ Record payment

function RecordPaymentModal({
  open,
  onClose,
  invoiceId,
  outstandingPaise,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  invoiceId: number;
  outstandingPaise: number;
  onDone: () => void;
}) {
  const toast = useToast();
  const record = useRecordPayment();
  const [amount, setAmount] = useState(paiseToInput(outstandingPaise));
  const [receivedOn, setReceivedOn] = useState(today());
  const [mode, setMode] = useState('');
  const [reference, setReference] = useState('');
  const [note, setNote] = useState('');
  const [serverError, setServerError] = useState<string | null>(null);

  // Reset the amount to the current outstanding whenever the modal (re)opens.
  const [seededFor, setSeededFor] = useState<number | null>(null);
  if (open && seededFor !== outstandingPaise) {
    setSeededFor(outstandingPaise);
    setAmount(paiseToInput(outstandingPaise));
    setReceivedOn(today());
    setMode('');
    setReference('');
    setNote('');
    setServerError(null);
  }
  if (!open && seededFor !== null) setSeededFor(null);

  const paise = rupeesToPaise(amount);
  const amountValid = paise !== null && paise > 0;

  function submit() {
    if (!amountValid || paise === null) return;
    setServerError(null);
    record.mutate(
      {
        invoice_id: invoiceId,
        amount_paise: paise,
        received_on: receivedOn || undefined,
        mode: mode.trim() || undefined,
        reference: reference.trim() || undefined,
        note: note.trim() || undefined,
      },
      {
        onSuccess: () => {
          toast.success('Payment recorded.');
          onDone();
        },
        onError: (e) => {
          // Surface the server's over-outstanding 422 (and any other error) honestly.
          setServerError(e instanceof ApiError ? e.message : 'Could not record the payment.');
        },
      },
    );
  }

  return (
    <Modal
      open={open}
      title="Record payment"
      onClose={onClose}
      busy={record.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={record.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={record.isPending} disabled={!amountValid}>
            Record payment
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="Amount (₹)"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          inputMode="decimal"
          required
          error={amount && !amountValid ? 'Enter a valid amount.' : undefined}
          hint={`Outstanding: ${rupees(outstandingPaise)}`}
        />
        <TextField
          label="Received on"
          type="date"
          value={receivedOn}
          onChange={(e) => setReceivedOn(e.target.value)}
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField
            label="Mode"
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            placeholder="NEFT / UPI / Cheque"
            maxLength={40}
          />
          <TextField
            label="Reference"
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            placeholder="UTR / cheque no."
            maxLength={120}
          />
        </div>
        <TextField
          label="Note"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={500}
        />
        {serverError && (
          <p role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {serverError}
          </p>
        )}
      </div>
    </Modal>
  );
}

// ============================================================ Apply advance

function ApplyAdvanceModal({
  open,
  onClose,
  invoiceId,
  clientId,
  outstandingPaise,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  invoiceId: number;
  clientId: number;
  outstandingPaise: number;
  onDone: () => void;
}) {
  const toast = useToast();
  const apply = useApplyAdvance();
  const suggestionQuery = useAdvanceSuggestion(open ? invoiceId : null);
  const advancesQuery = useAdvancesQuery(open ? String(clientId) : '');

  const [advanceId, setAdvanceId] = useState<string>('');
  const [amount, setAmount] = useState('');
  const [serverError, setServerError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  // Pre-fill from the FIFO suggestion's first line — but only until the operator edits,
  // so their override is never clobbered by a late-arriving query.
  const suggestion = suggestionQuery.data ?? [];
  const [seeded, setSeeded] = useState(false);
  if (open && !touched && !seeded && suggestion.length > 0) {
    setSeeded(true);
    setAdvanceId(String(suggestion[0].advance_id));
    setAmount(paiseToInput(suggestion[0].amount_paise));
  }
  if (!open && (seeded || touched || serverError)) {
    setSeeded(false);
    setTouched(false);
    setServerError(null);
    setAdvanceId('');
    setAmount('');
  }

  const advances = advancesQuery.data ?? [];
  const selected = advances.find((a) => String(a.advance_id) === advanceId);
  const paise = rupeesToPaise(amount);
  const valid = advanceId !== '' && paise !== null && paise > 0;

  function submit() {
    if (!valid || paise === null) return;
    setServerError(null);
    apply.mutate(
      { advance_id: Number(advanceId), invoice_id: invoiceId, amount_paise: paise },
      {
        onSuccess: () => {
          toast.success('Advance applied.');
          onDone();
        },
        onError: (e) => {
          setServerError(e instanceof ApiError ? e.message : 'Could not apply the advance.');
        },
      },
    );
  }

  const hasAdvances = advances.length > 0;

  return (
    <Modal
      open={open}
      title="Apply advance"
      onClose={onClose}
      busy={apply.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={apply.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={apply.isPending} disabled={!valid}>
            Apply advance
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {suggestionQuery.isSuccess && suggestion.length > 0 && (
          <p className="rounded-md border border-brand-100 bg-brand-50 px-3 py-2 text-xs text-brand-700">
            Suggested (FIFO): {rupees(suggestion[0].amount_paise)} from the oldest advance. You can
            override the advance and amount below.
          </p>
        )}

        {advancesQuery.isPending ? (
          <Loading label="Loading advances…" />
        ) : advancesQuery.isError ? (
          <ErrorState error={advancesQuery.error} onRetry={() => void advancesQuery.refetch()} />
        ) : !hasAdvances ? (
          <StatePanel title="No advances available">
            This client has no advances with a remaining balance.
          </StatePanel>
        ) : (
          <>
            <SelectField
              label="Advance"
              value={advanceId}
              required
              onChange={(e) => {
                setTouched(true);
                setAdvanceId(e.target.value);
              }}
            >
              <option value="">Select an advance…</option>
              {advances.map((a) => (
                <option key={a.advance_id} value={a.advance_id}>
                  {[a.reference, formatDate(a.received_on)].filter(Boolean).join(' · ') ||
                    `Advance #${a.advance_id}`}{' '}
                  — remaining {rupees(a.remaining_paise)}
                </option>
              ))}
            </SelectField>
            <TextField
              label="Amount (₹)"
              value={amount}
              inputMode="decimal"
              required
              onChange={(e) => {
                setTouched(true);
                setAmount(e.target.value);
              }}
              error={amount && (paise === null || paise <= 0) ? 'Enter a valid amount.' : undefined}
              hint={
                selected
                  ? `Advance remaining: ${rupees(selected.remaining_paise)} · Invoice outstanding: ${rupees(outstandingPaise)}`
                  : `Invoice outstanding: ${rupees(outstandingPaise)}`
              }
            />
          </>
        )}

        {serverError && (
          <p role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {serverError}
          </p>
        )}
      </div>
    </Modal>
  );
}
