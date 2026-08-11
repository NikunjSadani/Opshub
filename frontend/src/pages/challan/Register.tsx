import { useState } from 'react';
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
  useChallansQuery,
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
  const { download } = useApi();
  const isAdmin = user?.role === 'ADMIN';

  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');
  const [status, setStatus] = useState<ChallanStatus | ''>('');

  const filters: ChallanFilters = { series, fy, status };
  const query = useChallansQuery(filters);

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
      <PageHeader title="Register" subtitle="Issued challans across all batches." />

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
          onChange={(e) => setStatus(e.target.value as ChallanStatus | '')}
        >
          <option value="">All</option>
          <option value="ISSUED">Issued</option>
          <option value="VOID">Void</option>
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading register…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <StatePanel title="No challans found">
          No {status ? status.toLowerCase() : ''} challans match these filters.
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Number</Th>
              <Th>Date</Th>
              <Th>Consignee</Th>
              <Th>Ship-to state</Th>
              <Th>E-way</Th>
              <Th className="text-right">Total</Th>
              <Th>Status</Th>
              <Th className="text-right">Actions</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((c) => (
              <Tr key={c.id}>
                <Td className="font-medium text-slate-900">{c.number}</Td>
                <Td className="whitespace-nowrap">{formatDate(c.challan_date)}</Td>
                <Td>
                  <span className="text-slate-900">{c.consignee_brand}</span>
                  <span className="text-slate-400"> — </span>
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
