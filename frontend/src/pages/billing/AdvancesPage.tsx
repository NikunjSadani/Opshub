import { useMemo, useState } from 'react';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  SearchableSelect,
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
import { rupees } from './billingFormat';
import {
  rupeesToPaise,
  useAdvancesQuery,
  useRecordAdvance,
  type Advance,
} from '../../api/billingAr';

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

type ApplyState = { tone: 'green' | 'amber' | 'slate'; label: string };

/** Derive an advance's application status from its remaining vs total. */
function applyState(a: Advance): ApplyState {
  if (a.remaining_paise >= a.amount_paise) return { tone: 'green', label: 'Available' };
  if (a.remaining_paise <= 0) return { tone: 'slate', label: 'Fully applied' };
  return { tone: 'amber', label: 'Partly applied' };
}

/**
 * Advances — client deposits with their applied/remaining split. Filter by client,
 * record a new advance (OPERATE), and see how much of each advance is still available.
 *
 * NOTE ON UN-APPLY: the backend exposes advance→invoice applications only per INVOICE
 * (`GET /billing/invoices/{id}/ar` → `applied_advances`, each with the application id
 * the DELETE needs) — there is no per-advance applications endpoint. So reversing a
 * specific application is done from that invoice's Receivables detail (which lists each
 * applied advance with an "Un-apply" control). Here we surface how much of each advance
 * is applied and point the operator there.
 */
export function AdvancesPage() {
  const perms = usePermissions();
  const canOperate = perms.atLeast('billing', 'OPERATE');

  const [clientId, setClientId] = useState('');
  const [recordOpen, setRecordOpen] = useState(false);

  const clientsQuery = useClientsQuery();
  const clientLabel = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of clientsQuery.data ?? []) m.set(c.id, `${c.code} — ${c.name}`);
    return m;
  }, [clientsQuery.data]);

  const query = useAdvancesQuery(clientId);
  const rows = query.data ?? [];

  return (
    <div>
      <PageHeader
        title="Advances"
        subtitle="Client deposits and how much of each remains to be applied."
        actions={
          canOperate ? (
            <Button size="sm" onClick={() => setRecordOpen(true)}>
              Record advance
            </Button>
          ) : undefined
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SearchableSelect
          label="Client"
          value={clientId}
          onChange={setClientId}
          error={clientsQuery.isError ? "Couldn't load clients." : undefined}
          noneLabel={clientsQuery.isError ? 'Failed to load clients' : 'All clients'}
          placeholder="Search a client…"
          options={(clientsQuery.data ?? []).map((c: Client) => ({
            value: String(c.id),
            label: `${c.code} — ${c.name}`,
          }))}
        />
      </div>

      {query.isPending ? (
        <Loading label="Loading advances…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No advances found">
          {clientId ? 'This client has no recorded advances.' : 'No advances recorded yet.'}
        </StatePanel>
      ) : (
        <>
          <p className="mb-3 text-xs text-slate-500">
            To reverse a specific application, open the invoice under Receivables and use its
            “Un-apply” control.
          </p>
          <Table>
            <THead>
              <Tr>
                <Th>Received</Th>
                <Th>Client</Th>
                <Th>Reference</Th>
                <Th>Mode</Th>
                <Th className="text-right">Amount</Th>
                <Th className="text-right">Applied</Th>
                <Th className="text-right">Remaining</Th>
                <Th>Status</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((a) => {
                const st = applyState(a);
                return (
                  <Tr key={a.advance_id}>
                    <Td className="whitespace-nowrap">{formatDate(a.received_on)}</Td>
                    <Td>{clientLabel.get(String(a.client_id)) ?? `Client #${a.client_id}`}</Td>
                    <Td>{a.reference ?? '—'}</Td>
                    <Td>{a.mode ?? '—'}</Td>
                    <Td className="text-right tabular-nums">{rupees(a.amount_paise)}</Td>
                    <Td className="text-right tabular-nums">{rupees(a.applied_paise)}</Td>
                    <Td className="text-right tabular-nums font-medium text-slate-900">
                      {rupees(a.remaining_paise)}
                    </Td>
                    <Td>
                      <Badge tone={st.tone}>{st.label}</Badge>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
          <p className="mt-4 text-sm text-slate-500">Showing {rows.length}</p>
        </>
      )}

      {canOperate && (
        <RecordAdvanceModal
          open={recordOpen}
          onClose={() => setRecordOpen(false)}
          onDone={() => setRecordOpen(false)}
          defaultClientId={clientId}
        />
      )}
    </div>
  );
}

// ============================================================ Record advance

function RecordAdvanceModal({
  open,
  onClose,
  onDone,
  defaultClientId,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void;
  defaultClientId: string;
}) {
  const toast = useToast();
  const record = useRecordAdvance();
  const clientsQuery = useClientsQuery();

  const [clientId, setClientId] = useState(defaultClientId);
  const [amount, setAmount] = useState('');
  const [receivedOn, setReceivedOn] = useState(today());
  const [mode, setMode] = useState('');
  const [reference, setReference] = useState('');
  const [note, setNote] = useState('');
  const [serverError, setServerError] = useState<string | null>(null);

  // Re-seed each time the modal opens (pick up the current list filter as the default).
  const [wasOpen, setWasOpen] = useState(false);
  if (open && !wasOpen) {
    setWasOpen(true);
    setClientId(defaultClientId);
    setAmount('');
    setReceivedOn(today());
    setMode('');
    setReference('');
    setNote('');
    setServerError(null);
  }
  if (!open && wasOpen) setWasOpen(false);

  const paise = rupeesToPaise(amount);
  const amountValid = paise !== null && paise > 0;
  const valid = clientId !== '' && amountValid;

  function submit() {
    if (!valid || paise === null) return;
    setServerError(null);
    record.mutate(
      {
        client_id: Number(clientId),
        amount_paise: paise,
        received_on: receivedOn || undefined,
        mode: mode.trim() || undefined,
        reference: reference.trim() || undefined,
        note: note.trim() || undefined,
      },
      {
        onSuccess: () => {
          toast.success('Advance recorded.');
          onDone();
        },
        onError: (e) => {
          setServerError(e instanceof ApiError ? e.message : 'Could not record the advance.');
        },
      },
    );
  }

  return (
    <Modal
      open={open}
      title="Record advance"
      onClose={onClose}
      busy={record.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={record.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={record.isPending} disabled={!valid}>
            Record advance
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <SearchableSelect
          label="Client"
          value={clientId}
          required
          onChange={setClientId}
          error={clientsQuery.isError ? "Couldn't load clients." : undefined}
          placeholder="Select a client…"
          options={(clientsQuery.data ?? []).map((c: Client) => ({
            value: String(c.id),
            label: `${c.code} — ${c.name}`,
          }))}
        />
        <TextField
          label="Amount (₹)"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          inputMode="decimal"
          required
          error={amount && !amountValid ? 'Enter a valid amount.' : undefined}
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
