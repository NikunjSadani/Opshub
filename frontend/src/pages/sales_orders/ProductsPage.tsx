import { useEffect, useState, type ReactElement } from 'react';
import { Link, Navigate, Route, Routes } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
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
import { usePermissions } from '../../auth/AuthProvider';
import { ApiError } from '../../api/client';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import {
  useCreateProduct,
  useProductsQuery,
  useSyncHsnMaster,
  useUpdateProduct,
  type Product,
  type ProductFilters,
  type ProductsHsnSyncResult,
} from '../../api/products';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';
import { ProductsUpload } from './ProductsUpload';

/** Route base for the Products tab (nested under the Sales Orders module). */
const PRODUCTS_BASE = `${SALES_ORDERS_BASE}/products`;

/** Pull a human string out of any thrown value — never "[object Object]". */
function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

interface FormState {
  code: string;
  name: string;
  brand: string;
  model_number: string;
  category: string;
  uom: string;
  hsn: string;
  gst_rate: string;
  active: boolean;
}

const EMPTY_FORM: FormState = {
  code: '',
  name: '',
  brand: '',
  model_number: '',
  category: '',
  uom: 'PCS',
  hsn: '',
  gst_rate: '',
  active: true,
};

function formFrom(product: Product): FormState {
  return {
    code: product.code ?? '',
    name: product.name,
    brand: product.brand ?? '',
    model_number: product.model_number ?? '',
    category: product.category ?? '',
    uom: product.uom,
    hsn: product.hsn ?? '',
    // GST rate is a string on the wire (like `tax_rate`) — keep it as-is (no Number()) so it
    // round-trips without precision loss.
    gst_rate: product.gst_rate ?? '',
    active: product.active,
  };
}

/** An optional GST-rate field: blank is fine, otherwise a number 0..100 (mirrors the PO
 * line / project-pricing `taxRateValid`). */
function gstRateValid(text: string): boolean {
  if (text.trim() === '') return true;
  const n = Number(text.trim());
  return Number.isFinite(n) && n >= 0 && n <= 100;
}

/**
 * Create/edit modal. On create it POSTs a new product; on edit it PATCHes the
 * changed fields (including the active toggle, which is edit-only). A duplicate
 * identity/code surfaces the server's 409 message via toast.
 */
function ProductModal({
  open,
  editing,
  duplicateFrom,
  onClose,
}: {
  open: boolean;
  editing: Product | null;
  /** When set (and not editing), the modal opens in CREATE mode pre-filled from this product
   * — a "duplicate & edit" flow. The unique `code` is cleared so the copy needs its own. */
  duplicateFrom: Product | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const createProduct = useCreateProduct();
  const updateProduct = useUpdateProduct();
  const isEdit = editing !== null;
  const pending = createProduct.isPending || updateProduct.isPending;

  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  // Seed the form + clear mutation state whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    setForm(
      editing
        ? formFrom(editing)
        : duplicateFrom
          ? { ...formFrom(duplicateFrom), code: '' } // duplicate: prefill, but a fresh code
          : EMPTY_FORM,
    );
    createProduct.reset();
    updateProduct.reset();
    // Only re-run on open transitions; the mutation resets are stable enough here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing, duplicateFrom]);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  const trimmedName = form.name.trim();
  const trimmedUom = form.uom.trim();
  const gstErr = !gstRateValid(form.gst_rate);
  const canSubmit = trimmedName.length > 0 && trimmedUom.length > 0 && !gstErr && !pending;

  function close() {
    if (pending) return;
    onClose();
  }

  /** Optional text → trimmed value or undefined (so blanks aren't sent). */
  function opt(value: string): string | undefined {
    const t = value.trim();
    return t.length > 0 ? t : undefined;
  }

  function submit() {
    if (!canSubmit) return;
    if (isEdit) {
      updateProduct.mutate(
        {
          id: editing.id,
          code: form.code.trim() || undefined,
          name: trimmedName,
          brand: form.brand.trim() || undefined,
          model_number: form.model_number.trim() || undefined,
          category: form.category.trim() || undefined,
          uom: trimmedUom,
          hsn: form.hsn.trim() || undefined,
          // Empty GST clears it on edit (explicit null); a value sends the trimmed string.
          gst_rate: form.gst_rate.trim() || null,
          active: form.active,
        },
        {
          onSuccess: (p) => {
            toast.success(`${p.name} updated.`);
            onClose();
          },
          onError: (err) => toast.error(errorMessage(err)),
        },
      );
    } else {
      createProduct.mutate(
        {
          code: opt(form.code),
          name: trimmedName,
          brand: opt(form.brand),
          model_number: opt(form.model_number),
          category: opt(form.category),
          uom: trimmedUom,
          hsn: opt(form.hsn),
          gst_rate: opt(form.gst_rate),
        },
        {
          onSuccess: (p) => {
            toast.success(`Product ${p.name} created.`);
            onClose();
          },
          onError: (err) => toast.error(errorMessage(err)),
        },
      );
    }
  }

  return (
    <Modal
      open={open}
      title={isEdit ? 'Edit product' : duplicateFrom ? 'Duplicate product' : 'New product'}
      onClose={close}
      busy={pending}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={pending} disabled={!canSubmit}>
            {isEdit ? 'Save changes' : 'Create product'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="Name"
          required
          value={form.name}
          onChange={(e) => set('name', e.target.value)}
          placeholder="e.g. 15W LED Bulb"
          maxLength={200}
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField
            label="Code"
            value={form.code}
            onChange={(e) => set('code', e.target.value)}
            placeholder="Optional SKU / import code"
            maxLength={64}
          />
          <TextField
            label="UOM"
            required
            value={form.uom}
            onChange={(e) => set('uom', e.target.value)}
            hint="Unit of measure (e.g. PCS, BOX, KG)."
            maxLength={16}
          />
          <TextField
            label="Brand"
            value={form.brand}
            onChange={(e) => set('brand', e.target.value)}
            maxLength={120}
          />
          <TextField
            label="Model number"
            value={form.model_number}
            onChange={(e) => set('model_number', e.target.value)}
            maxLength={120}
          />
          <TextField
            label="Category"
            value={form.category}
            onChange={(e) => set('category', e.target.value)}
            maxLength={120}
          />
          <TextField
            label="HSN / SAC"
            value={form.hsn}
            onChange={(e) => set('hsn', e.target.value)}
            maxLength={16}
          />
          <TextField
            label="GST rate (%)"
            inputMode="decimal"
            value={form.gst_rate}
            onChange={(e) => set('gst_rate', e.target.value)}
            hint="Optional. 0–100, up to 2 decimals (e.g. 18)."
            error={gstErr ? 'Enter a GST rate from 0 to 100.' : undefined}
          />
        </div>
        {isEdit && (
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(e) => set('active', e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-2 focus:ring-brand-500/40"
            />
            Active (uncheck to retire this product)
          </label>
        )}
      </div>
    </Modal>
  );
}

/**
 * Result modal for "Sync HSN master from products". Shows the created / rate-corrected
 * counts and — most importantly — surfaces CONFLICTS prominently (the operator-actionable
 * part): each HSN whose active products disagree on the rate, so it can be fixed at source.
 */
function HsnSyncModal({
  open,
  result,
  onClose,
}: {
  open: boolean;
  result: ProductsHsnSyncResult | null;
  onClose: () => void;
}) {
  const created = result?.created.length ?? 0;
  const updated = result?.updated.length ?? 0;
  const conflicts = result?.conflicts ?? [];

  return (
    <Modal
      open={open}
      title="HSN master sync"
      onClose={onClose}
      footer={
        <Button onClick={onClose}>Done</Button>
      }
    >
      {result == null ? null : (
        <div className="space-y-5">
          <p className="text-sm text-slate-700">
            {created} HSN code{created === 1 ? '' : 's'} created, {updated} rate
            {updated === 1 ? '' : 's'} updated
            {conflicts.length > 0
              ? `, ${conflicts.length} conflict${conflicts.length === 1 ? '' : 's'} to resolve.`
              : '.'}
          </p>

          <div className="flex flex-wrap gap-4 text-sm">
            <span className="text-emerald-700">Created: {created}</span>
            <span className="text-blue-700">Updated: {updated}</span>
            <span className="text-rose-700">Conflicts: {conflicts.length}</span>
          </div>

          {result.created.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Created
              </h3>
              <div className="flex flex-wrap gap-2">
                {result.created.map((hsn) => (
                  <Badge key={hsn} tone="green">
                    {hsn}
                  </Badge>
                ))}
              </div>
            </section>
          )}

          {result.updated.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Rates corrected
              </h3>
              <ul className="divide-y divide-slate-100 rounded-xl border border-slate-200">
                {result.updated.map((u) => (
                  <li key={u.hsn} className="px-3 py-2 text-sm">
                    <span className="font-mono font-medium text-slate-900">{u.hsn}</span>
                    <span className="text-slate-600">
                      {' '}
                      — {u.old_rate}% → {u.new_rate}%
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Conflicts are the operator-actionable part — surfaced prominently, never buried. */}
          {conflicts.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-rose-700">
                Conflicts — fix the product data
              </h3>
              <ul className="divide-y divide-rose-100 rounded-xl border border-rose-200 bg-rose-50/40">
                {conflicts.map((c) => (
                  <li key={c.hsn} className="px-3 py-2 text-sm">
                    <span className="font-mono font-medium text-slate-900">HSN {c.hsn}</span>
                    <span className="text-slate-700">
                      {' '}
                      has conflicting rates {c.rates.join(', ')} across{' '}
                      {c.product_ids.length} product{c.product_ids.length === 1 ? '' : 's'} — the
                      most recent was applied; fix the product data.
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {created === 0 && updated === 0 && conflicts.length === 0 && (
            <StatePanel title="Nothing to sync">
              No products carry both an HSN and a GST rate yet.
            </StatePanel>
          )}
        </div>
      )}
    </Modal>
  );
}

/**
 * Products tab shell: owns its own list / upload sub-routes (mounted at
 * `sales_orders/products/*`). Server-side RBAC is the real gate; the create + upload
 * affordances and the upload route are gated at MANAGE here for honest UX.
 */
export function ProductsPage() {
  const perms = usePermissions();
  const canManage = perms.atLeast('sales_orders', 'MANAGE');
  // Defer the MANAGE route guard until /me resolves so a deep-link isn't bounced before
  // permissions load (mirrors PurchaseOrdersPage's OPERATE guard).
  const manageGuard = (node: ReactElement) =>
    perms.loading ? (
      <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
    ) : canManage ? (
      node
    ) : (
      <Navigate to={PRODUCTS_BASE} replace />
    );

  return (
    <Routes>
      <Route index element={<ProductsRegister canManage={canManage} />} />
      <Route path="upload" element={manageGuard(<ProductsUpload />)} />
      <Route path="*" element={<Navigate to={PRODUCTS_BASE} replace />} />
    </Routes>
  );
}

/** Product Master: searchable list + a MANAGE-gated create/edit modal. */
function ProductsRegister({ canManage }: { canManage: boolean }) {
  const toast = useToast();
  const [q, setQ] = useState('');
  const [category, setCategory] = useState('');
  const [active, setActive] = useState<'true' | 'false' | ''>('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Product | null>(null);
  const [duplicating, setDuplicating] = useState<Product | null>(null);

  // "Sync HSN master from products" (MANAGE): rebuild the challan HSN master from product
  // GST rates, then surface the created / updated / conflicting outcome in a modal.
  const syncHsn = useSyncHsnMaster();
  const [syncResult, setSyncResult] = useState<ProductsHsnSyncResult | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);

  function runSyncHsn() {
    if (syncHsn.isPending) return;
    syncHsn.mutate(undefined, {
      onSuccess: (out) => {
        setSyncResult(out);
        setSyncOpen(true);
        const c = out.created.length;
        const u = out.updated.length;
        const k = out.conflicts.length;
        if (k > 0) {
          toast.info(
            `${c} HSN created, ${u} updated — ${k} conflict${k === 1 ? '' : 's'} need attention.`,
          );
        } else {
          toast.success(
            `${c} HSN code${c === 1 ? '' : 's'} created, ${u} rate${u === 1 ? '' : 's'} updated.`,
          );
        }
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  // Debounce the free-text search that feeds the query key (category/active are
  // selects/immediate) so each keystroke does not fire its own request.
  const filters: ProductFilters = { q: useDebouncedValue(q), category, active };
  const query = useProductsQuery(filters);
  const rows = query.data ?? [];

  function openCreate() {
    setEditing(null);
    setDuplicating(null);
    setModalOpen(true);
  }

  function openEdit(product: Product) {
    setEditing(product);
    setDuplicating(null);
    setModalOpen(true);
  }

  // Duplicate & edit: open the create modal pre-filled from this product (fresh code).
  function openDuplicate(product: Product) {
    setEditing(null);
    setDuplicating(product);
    setModalOpen(true);
  }

  return (
    <div>
      <PageHeader
        title="Products"
        subtitle="The product master — every item that can appear on a sales or purchase order."
        actions={
          canManage ? (
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={runSyncHsn}
                loading={syncHsn.isPending}
              >
                Sync HSN master
              </Button>
              <Link to={`${PRODUCTS_BASE}/upload`}>
                <Button variant="secondary" size="sm">
                  Upload .xlsx
                </Button>
              </Link>
              <Button size="sm" onClick={openCreate}>
                New product
              </Button>
            </div>
          ) : undefined
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Name, code, brand…"
          maxLength={80}
        />
        <TextField
          label="Category"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          placeholder="Filter by category"
          maxLength={80}
        />
        <SelectField
          label="Status"
          value={active}
          onChange={(e) => setActive(e.target.value as 'true' | 'false' | '')}
        >
          <option value="">All</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading products…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No products found">
          {q.trim() || category.trim() || active
            ? 'No products match these filters.'
            : canManage
              ? 'Add a product to get started.'
              : 'No products have been added yet.'}
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Code</Th>
              <Th>Name</Th>
              <Th>Brand</Th>
              <Th>Category</Th>
              <Th>UOM</Th>
              <Th>HSN</Th>
              <Th>GST %</Th>
              <Th>Status</Th>
              {canManage && <Th className="text-right">Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {rows.map((p) => (
              <Tr key={p.id}>
                <Td className="font-mono text-slate-900">{p.code ?? '—'}</Td>
                <Td className="font-medium text-slate-900">{p.name}</Td>
                <Td>{p.brand ?? '—'}</Td>
                <Td>{p.category ?? '—'}</Td>
                <Td className="whitespace-nowrap">{p.uom}</Td>
                <Td className="tabular-nums">{p.hsn ?? '—'}</Td>
                <Td className="tabular-nums">{p.gst_rate != null ? `${p.gst_rate}%` : '—'}</Td>
                <Td>
                  <Badge tone={p.active ? 'green' : 'slate'}>
                    {p.active ? 'Active' : 'Inactive'}
                  </Badge>
                </Td>
                {canManage && (
                  <Td>
                    <div className="flex justify-end gap-3">
                      <button
                        type="button"
                        onClick={() => openDuplicate(p)}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        Copy
                      </button>
                      <button
                        type="button"
                        onClick={() => openEdit(p)}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        Edit
                      </button>
                    </div>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {canManage && (
        <>
          <ProductModal
            open={modalOpen}
            editing={editing}
            duplicateFrom={duplicating}
            onClose={() => setModalOpen(false)}
          />
          <HsnSyncModal
            open={syncOpen}
            result={syncResult}
            onClose={() => setSyncOpen(false)}
          />
        </>
      )}
    </div>
  );
}
