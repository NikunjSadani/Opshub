import { useState, type FormEvent, type ReactNode } from 'react';
import {
  Badge,
  Button,
  Loading,
  ErrorState,
  StatePanel,
  PageHeader,
  SelectField,
  TextField,
  Table,
  THead,
  Th,
  Tr,
  Td,
  Modal,
  ConfirmDialog,
  useToast,
} from '../../../ui';
import { useDebouncedValue } from '../../../hooks/useDebouncedValue';
import {
  parseApiError,
  useMasterList,
  useMasterCreate,
  useMasterUpdate,
  useMasterToggle,
  type MasterKind,
} from '../../../api/masterdata';

/** Setter passed to `renderFields` to patch one or more form values. */
export type Patch<TInput> = (p: Partial<TInput>) => void;

export interface EntityManagerProps<TRow, TInput> {
  kind: MasterKind;
  title: string;
  subtitle: string;
  /** Singular noun for buttons/dialogs, e.g. "Consignor". */
  addLabel: string;
  emptyTitle: string;
  /** Column headers, in order, matching the cells from `renderCells`. */
  columns: string[];
  /** Extra text filters (e.g. ['brand','state'] for consignee). */
  filterKeys?: string[];
  blankInput: TInput;
  toInput: (row: TRow) => TInput;
  renderCells: (row: TRow) => ReactNode;
  renderFields: (form: TInput, patch: Patch<TInput>, errors: Record<string, string>) => ReactNode;
  rowId: (row: TRow) => number;
  rowActive: (row: TRow) => boolean;
  rowName: (row: TRow) => string;
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Reusable CRUD screen for one master-data entity: filterable list with explicit
 * loading/error/empty states, an Add/Edit modal, and a reversible enable/disable
 * toggle. All list reads and writes go through the typed react-query hooks.
 */
export function EntityManager<TRow, TInput>({
  kind,
  title,
  subtitle,
  addLabel,
  emptyTitle,
  columns,
  filterKeys = [],
  blankInput,
  toInput,
  renderCells,
  renderFields,
  rowId,
  rowActive,
  rowName,
}: EntityManagerProps<TRow, TInput>) {
  const toast = useToast();

  const [activeFilter, setActiveFilter] = useState<'all' | 'true' | 'false'>('all');
  const [extra, setExtra] = useState<Record<string, string>>(() =>
    Object.fromEntries(filterKeys.map((k) => [k, ''])),
  );

  // Debounce the free-text filters that feed the query key so each keystroke does
  // not fire its own request (the Status select is applied immediately).
  const debouncedExtra = useDebouncedValue(extra);
  const params: Record<string, string | undefined> = {
    active: activeFilter === 'all' ? undefined : activeFilter,
    ...debouncedExtra,
  };

  const list = useMasterList<TRow>(kind, params);
  const create = useMasterCreate<TInput>(kind);
  const update = useMasterUpdate<TInput>(kind);
  const toggle = useMasterToggle(kind);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<TRow | null>(null);
  const [form, setForm] = useState<TInput>(blankInput);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | undefined>();
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [confirmRow, setConfirmRow] = useState<TRow | null>(null);

  const patch: Patch<TInput> = (p) => setForm((f) => ({ ...f, ...p }) as TInput);

  function openCreate() {
    setEditing(null);
    setForm(blankInput);
    setErrors({});
    setFormError(undefined);
    setFormOpen(true);
  }

  function openEdit(row: TRow) {
    setEditing(row);
    setForm(toInput(row));
    setErrors({});
    setFormError(undefined);
    setFormOpen(true);
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    setFormError(undefined);

    const onError = (err: unknown) => {
      const parsed = parseApiError(err);
      setErrors(parsed.fieldErrors);
      if (parsed.formError) setFormError(parsed.formError);
    };
    const onSuccess = () => {
      toast.success(editing ? `${addLabel} updated` : `${addLabel} created`);
      setFormOpen(false);
    };

    if (editing) update.mutate({ id: rowId(editing), body: form }, { onSuccess, onError });
    else create.mutate(form, { onSuccess, onError });
  }

  function doToggle(row: TRow, next: boolean) {
    const id = rowId(row);
    setConfirmRow(null);
    setPendingId(id);
    toggle.mutate(
      { id, active: next },
      {
        onSuccess: () => toast.success(next ? `${rowName(row)} enabled` : `${rowName(row)} disabled`),
        onError: (err) => toast.error(parseApiError(err).formError ?? 'Could not update status'),
        onSettled: () => setPendingId(null),
      },
    );
  }

  const submitting = editing ? update.isPending : create.isPending;
  const rows = list.data ?? [];

  return (
    <div>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={<Button onClick={openCreate}>Add {addLabel}</Button>}
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        {filterKeys.map((k) => (
          <TextField
            key={k}
            label={`Filter by ${titleCase(k)}`}
            value={extra[k] ?? ''}
            placeholder="All"
            onChange={(e) => setExtra((x) => ({ ...x, [k]: e.target.value }))}
            className="w-44"
          />
        ))}
        <SelectField
          label="Status"
          value={activeFilter}
          onChange={(e) => setActiveFilter(e.target.value as 'all' | 'true' | 'false')}
          className="w-44"
        >
          <option value="all">All</option>
          <option value="true">Active only</option>
          <option value="false">Inactive only</option>
        </SelectField>
      </div>

      {list.isLoading ? (
        <Loading />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title={emptyTitle}>Use “Add {addLabel}” to create the first one.</StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              {columns.map((c) => (
                <Th key={c}>{c}</Th>
              ))}
              <Th>Status</Th>
              <Th className="text-right">Actions</Th>
            </Tr>
          </THead>
          <tbody>
            {rows.map((row) => (
              <Tr key={rowId(row)}>
                {renderCells(row)}
                <Td>
                  {rowActive(row) ? (
                    <Badge tone="green">Active</Badge>
                  ) : (
                    <Badge tone="slate">Inactive</Badge>
                  )}
                </Td>
                <Td className="whitespace-nowrap text-right">
                  <Button size="sm" variant="secondary" onClick={() => openEdit(row)}>
                    Edit
                  </Button>
                  {rowActive(row) ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-2 text-rose-600 hover:bg-rose-50"
                      loading={pendingId === rowId(row)}
                      onClick={() => setConfirmRow(row)}
                    >
                      Disable
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-2 text-emerald-600 hover:bg-emerald-50"
                      loading={pendingId === rowId(row)}
                      onClick={() => doToggle(row, true)}
                    >
                      Enable
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
        title={editing ? `Edit ${addLabel}` : `Add ${addLabel}`}
        onClose={() => setFormOpen(false)}
        busy={submitting}
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {formError && (
            <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
              {formError}
            </div>
          )}
          {renderFields(form, patch, errors)}
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
        title={`Disable this ${addLabel.toLowerCase()}?`}
        message={
          confirmRow ? (
            <>
              <strong>{rowName(confirmRow)}</strong> will be hidden from new challans, but stays on
              record and can be re-enabled at any time.
            </>
          ) : null
        }
        confirmLabel="Disable"
        danger
        loading={pendingId !== null}
        onConfirm={() => confirmRow && doToggle(confirmRow, false)}
        onCancel={() => setConfirmRow(null)}
      />
    </div>
  );
}
