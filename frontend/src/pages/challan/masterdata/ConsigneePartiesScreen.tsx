import { useState, type FormEvent } from 'react';
import { useAuth } from '../../../auth/AuthProvider';
import {
  Badge,
  Button,
  Loading,
  ErrorState,
  StatePanel,
  PageHeader,
  TextField,
  Table,
  THead,
  Th,
  Tr,
  Td,
  Modal,
  useToast,
} from '../../../ui';
import { ApiError } from '../../../api/client';
import { parseApiError } from '../../../api/masterdata';
import {
  useConsigneePartiesQuery,
  useCreateConsigneeParty,
  useUpdateConsigneeParty,
  type ConsigneeParty,
} from '../../../api/consigneeMaster';

/** Editable form shape (all string-backed inputs). gstin is only writable on create. */
interface FormState {
  gstin: string;
  name: string;
  address_line1: string;
  address_line2: string;
  pincode: string;
  state: string;
  phone: string;
}

const BLANK: FormState = {
  gstin: '',
  name: '',
  address_line1: '',
  address_line2: '',
  pincode: '',
  state: '',
  phone: '',
};

const GSTIN_LEN = 15;

/**
 * Format an ISO timestamp's date part directly (slice, don't `new Date()`), so a
 * UTC parse can't shift the day in timezones behind UTC. Falls back to the raw
 * value / an em-dash.
 */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Turn a thrown error into a short, human toast line (409 duplicate / 422 invalid gstin). */
function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return 'A consignee with this GSTIN already exists.';
    if (err.status === 422) {
      const parsed = parseApiError(err);
      return parsed.fieldErrors.gstin ?? parsed.formError ?? 'That GSTIN is invalid — check the 15-character code.';
    }
  }
  return parseApiError(err).formError ?? 'Something went wrong. Please try again.';
}

/**
 * GSTIN-keyed Consignee Master: a searchable registry of bill-to parties with an
 * admin create/edit modal. The GSTIN is immutable after creation (shown read-only
 * when editing); the backend owns the checksum + uniqueness validation.
 */
export function ConsigneePartiesScreen() {
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';

  const [q, setQ] = useState('');
  const list = useConsigneePartiesQuery(q);
  const create = useCreateConsigneeParty();
  const update = useUpdateConsigneeParty();

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ConsigneeParty | null>(null);
  const [form, setForm] = useState<FormState>(BLANK);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }));

  function openCreate() {
    setEditing(null);
    setForm(BLANK);
    setErrors({});
    setFormOpen(true);
  }

  function openEdit(row: ConsigneeParty) {
    setEditing(row);
    setForm({
      gstin: row.gstin,
      name: row.name,
      address_line1: row.address_line1 ?? '',
      address_line2: row.address_line2 ?? '',
      pincode: row.pincode ?? '',
      state: row.state ?? '',
      phone: row.phone ?? '',
    });
    setErrors({});
    setFormOpen(true);
  }

  // Submit is disabled until the client-side minimums hold: a 15-char GSTIN (create
  // only — the backend runs the real checksum) and a non-empty name.
  const nameOk = form.name.trim().length > 0;
  const gstinOk = editing !== null || form.gstin.trim().length === GSTIN_LEN;
  const canSubmit = nameOk && gstinOk;

  const submitting = editing ? update.isPending : create.isPending;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setErrors({});

    const onError = (err: unknown) => {
      setErrors(parseApiError(err).fieldErrors);
      toast.error(friendlyError(err));
    };
    const onSuccess = () => {
      toast.success(editing ? 'Consignee updated' : 'Consignee created');
      setFormOpen(false);
    };

    if (editing) {
      const { gstin: _gstin, ...body } = form;
      update.mutate({ id: editing.id, body }, { onSuccess, onError });
    } else {
      create.mutate({ ...form }, { onSuccess, onError });
    }
  }

  const rows = list.data ?? [];

  return (
    <div>
      <PageHeader
        title="Consignees (GSTIN)"
        subtitle="GSTIN-keyed bill-to party master. Search by GSTIN, name or state."
        actions={isAdmin ? <Button onClick={openCreate}>Add consignee</Button> : undefined}
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <TextField
          label="Search"
          value={q}
          placeholder="GSTIN, name, state…"
          onChange={(e) => setQ(e.target.value)}
          className="w-72"
        />
      </div>

      {list.isLoading ? (
        <Loading />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title={q.trim() ? 'No matches' : 'No consignees yet'}>
          {q.trim()
            ? 'No consignee matches that search. Try a different term.'
            : isAdmin
              ? 'Use “Add consignee” to create the first one.'
              : 'None have been added yet.'}
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>GSTIN</Th>
              <Th>Name</Th>
              <Th>State</Th>
              <Th>Pincode</Th>
              <Th>Source</Th>
              <Th>Updated</Th>
              {isAdmin && <Th className="text-right">Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {rows.map((row) => (
              <Tr key={row.id}>
                <Td className="font-mono text-xs text-slate-900">{row.gstin}</Td>
                <Td className="font-medium text-slate-900">{row.name}</Td>
                <Td>{row.state || '—'}</Td>
                <Td>{row.pincode || '—'}</Td>
                <Td>
                  <Badge tone={row.source === 'UPLOAD' ? 'blue' : 'slate'}>{row.source}</Badge>
                </Td>
                <Td className="whitespace-nowrap">{formatDate(row.updated_at)}</Td>
                {isAdmin && (
                  <Td className="whitespace-nowrap text-right">
                    <Button size="sm" variant="secondary" onClick={() => openEdit(row)}>
                      Edit
                    </Button>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      <Modal
        open={formOpen}
        title={editing ? 'Edit consignee' : 'Add consignee'}
        onClose={() => setFormOpen(false)}
        busy={submitting}
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {editing ? (
            <TextField
              label="GSTIN"
              value={form.gstin}
              readOnly
              disabled
              className="font-mono"
              hint="The GSTIN is the party's key and cannot be changed."
            />
          ) : (
            <TextField
              label="GSTIN"
              required
              maxLength={GSTIN_LEN}
              className="font-mono"
              value={form.gstin}
              error={errors.gstin}
              hint="15 characters. The server verifies the checksum on save."
              onChange={(e) => patch({ gstin: e.target.value.toUpperCase() })}
            />
          )}
          <TextField
            label="Name"
            required
            value={form.name}
            error={errors.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
          <TextField
            label="Address line 1"
            value={form.address_line1}
            error={errors.address_line1}
            onChange={(e) => patch({ address_line1: e.target.value })}
          />
          <TextField
            label="Address line 2"
            value={form.address_line2}
            error={errors.address_line2}
            onChange={(e) => patch({ address_line2: e.target.value })}
          />
          <div className="grid grid-cols-2 gap-3">
            <TextField
              label="Pincode"
              value={form.pincode}
              error={errors.pincode}
              onChange={(e) => patch({ pincode: e.target.value })}
            />
            <TextField
              label="State"
              value={form.state}
              error={errors.state}
              onChange={(e) => patch({ state: e.target.value })}
            />
          </div>
          <TextField
            label="Phone"
            value={form.phone}
            error={errors.phone}
            onChange={(e) => patch({ phone: e.target.value })}
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
            <Button type="submit" loading={submitting} disabled={!canSubmit}>
              {editing ? 'Save changes' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
