import { useState, type FormEvent } from 'react';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
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
  usePaymentMethods,
  useCreatePaymentMethod,
  useUpdatePaymentMethod,
  type PaymentMethod,
} from '../../api/expense';
import { errorMessage } from './expenseFormat';

/**
 * Payment Methods admin (Manage-gated). Lists every payment method — active and
 * inactive — with Add, Rename, and a reversible Activate/Deactivate. There is no
 * hard delete: deactivating just hides a method from the upload picker while
 * keeping it on record (and on already-tagged invoices). The route is guarded in
 * ExpenseModule; server-side RBAC is the real gate.
 */
export function PaymentMethods() {
  const toast = useToast();
  const list = usePaymentMethods(false);
  const create = useCreatePaymentMethod();
  const update = useUpdatePaymentMethod();

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<PaymentMethod | null>(null);
  const [name, setName] = useState('');
  const [formError, setFormError] = useState<string | undefined>();
  const [confirmRow, setConfirmRow] = useState<PaymentMethod | null>(null);
  // Which row's toggle is in flight, so only that row shows a spinner.
  const [pendingId, setPendingId] = useState<number | null>(null);

  const rows = list.data ?? [];
  const submitting = editing ? update.isPending : create.isPending;

  function openCreate() {
    setEditing(null);
    setName('');
    setFormError(undefined);
    setFormOpen(true);
  }

  function openEdit(row: PaymentMethod) {
    setEditing(row);
    setName(row.name);
    setFormError(undefined);
    setFormOpen(true);
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setFormError('Enter a name.');
      return;
    }
    setFormError(undefined);
    const onSuccess = () => {
      toast.success(editing ? 'Payment method renamed' : 'Payment method added');
      setFormOpen(false);
    };
    const onError = (err: unknown) => {
      setFormError(errorMessage(err));
      toast.error(errorMessage(err));
    };
    if (editing) update.mutate({ id: editing.id, body: { name: trimmed } }, { onSuccess, onError });
    else create.mutate({ name: trimmed }, { onSuccess, onError });
  }

  function doToggle(row: PaymentMethod, next: boolean) {
    setConfirmRow(null);
    setPendingId(row.id);
    update.mutate(
      { id: row.id, body: { active: next } },
      {
        onSuccess: () =>
          toast.success(next ? `${row.name} activated` : `${row.name} deactivated`),
        onError: (err) => toast.error(errorMessage(err)),
        onSettled: () => setPendingId(null),
      },
    );
  }

  return (
    <div>
      <PageHeader
        title="Payment methods"
        subtitle="Manage the list of payment methods invoices can be tagged with at upload."
        actions={<Button onClick={openCreate}>Add payment method</Button>}
      />

      {list.isPending ? (
        <Loading label="Loading payment methods…" />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No payment methods yet">
          Use “Add payment method” to create the first one.
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th className="text-right">Actions</Th>
            </Tr>
          </THead>
          <tbody>
            {rows.map((row) => (
              <Tr key={row.id}>
                <Td className="font-medium text-slate-900">{row.name}</Td>
                <Td>
                  {row.active ? (
                    <Badge tone="green">Active</Badge>
                  ) : (
                    <Badge tone="slate">Inactive</Badge>
                  )}
                </Td>
                <Td className="whitespace-nowrap text-right">
                  <Button size="sm" variant="secondary" onClick={() => openEdit(row)}>
                    Rename
                  </Button>
                  {row.active ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-2 text-rose-600 hover:bg-rose-50"
                      loading={pendingId === row.id}
                      onClick={() => setConfirmRow(row)}
                    >
                      Deactivate
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-2 text-emerald-600 hover:bg-emerald-50"
                      loading={pendingId === row.id}
                      onClick={() => doToggle(row, true)}
                    >
                      Activate
                    </Button>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      <Modal
        open={formOpen}
        title={editing ? 'Rename payment method' : 'Add payment method'}
        onClose={() => setFormOpen(false)}
        busy={submitting}
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {formError && (
            <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
              {formError}
            </div>
          )}
          <TextField
            label="Name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Bank transfer"
            maxLength={80}
            autoFocus
          />
          <div className="flex justify-end gap-2 pt-1">
            <Button
              type="button"
              variant="secondary"
              onClick={() => setFormOpen(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" loading={submitting}>
              {editing ? 'Save changes' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={confirmRow !== null}
        title="Deactivate this payment method?"
        message={
          confirmRow ? (
            <>
              <strong>{confirmRow.name}</strong> will be hidden from the upload picker, but stays on
              record (and on any invoices already tagged with it) and can be reactivated at any time.
            </>
          ) : null
        }
        confirmLabel="Deactivate"
        danger
        loading={pendingId !== null}
        onConfirm={() => confirmRow && doToggle(confirmRow, false)}
        onCancel={() => setConfirmRow(null)}
      />
    </div>
  );
}
