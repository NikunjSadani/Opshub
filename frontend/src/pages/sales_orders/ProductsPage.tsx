import { useEffect, useState } from 'react';
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
  useUpdateProduct,
  type Product,
  type ProductFilters,
} from '../../api/products';

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
    active: product.active,
  };
}

/**
 * Create/edit modal. On create it POSTs a new product; on edit it PATCHes the
 * changed fields (including the active toggle, which is edit-only). A duplicate
 * identity/code surfaces the server's 409 message via toast.
 */
function ProductModal({
  open,
  editing,
  onClose,
}: {
  open: boolean;
  editing: Product | null;
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
    setForm(editing ? formFrom(editing) : EMPTY_FORM);
    createProduct.reset();
    updateProduct.reset();
    // Only re-run on open transitions; the mutation resets are stable enough here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  const trimmedName = form.name.trim();
  const trimmedUom = form.uom.trim();
  const canSubmit = trimmedName.length > 0 && trimmedUom.length > 0 && !pending;

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
      title={isEdit ? 'Edit product' : 'New product'}
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

/** Product Master: searchable list + a MANAGE-gated create/edit modal. */
export function ProductsPage() {
  const perms = usePermissions();
  // Reads need VIEW; create + edit need MANAGE (matches the backend).
  const canManage = perms.atLeast('sales_orders', 'MANAGE');

  const [q, setQ] = useState('');
  const [category, setCategory] = useState('');
  const [active, setActive] = useState<'true' | 'false' | ''>('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Product | null>(null);

  // Debounce the free-text search that feeds the query key (category/active are
  // selects/immediate) so each keystroke does not fire its own request.
  const filters: ProductFilters = { q: useDebouncedValue(q), category, active };
  const query = useProductsQuery(filters);
  const rows = query.data ?? [];

  function openCreate() {
    setEditing(null);
    setModalOpen(true);
  }

  function openEdit(product: Product) {
    setEditing(product);
    setModalOpen(true);
  }

  return (
    <div>
      <PageHeader
        title="Products"
        subtitle="The product master — every item that can appear on a sales or purchase order."
        actions={
          canManage ? (
            <Button size="sm" onClick={openCreate}>
              New product
            </Button>
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
                <Td>
                  <Badge tone={p.active ? 'green' : 'slate'}>
                    {p.active ? 'Active' : 'Inactive'}
                  </Badge>
                </Td>
                {canManage && (
                  <Td>
                    <div className="flex justify-end">
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
        <ProductModal open={modalOpen} editing={editing} onClose={() => setModalOpen(false)} />
      )}
    </div>
  );
}
