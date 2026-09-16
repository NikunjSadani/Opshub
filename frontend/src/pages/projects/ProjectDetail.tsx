import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
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
  TextField,
  Td,
  THead,
  Th,
  Tr,
  useToast,
  type SearchableSelectOption,
} from '../../ui';
import { ApiError } from '../../api/client';
import { usePermissions } from '../../auth/AuthProvider';
import { useProjectQuery } from '../../api/projects';
import {
  PO_STATUS_LABEL,
  PO_STATUS_TONE,
  rupeesToPaise,
  useProductSearch,
  usePurchaseOrdersQuery,
  type PickerProduct,
  type POStatus,
} from '../../api/purchaseOrders';
import {
  useProjectProductsQuery,
  useTagProduct,
  useUntagProduct,
  useUpdateProjectProduct,
  type ProjectProduct,
  type ProjectProductPricing,
} from '../../api/projectProducts';
import { SALES_ORDERS_BASE, rupees } from '../sales_orders/salesOrdersFormat';
import { formatDate, PROJECT_STATUS_LABEL, PROJECT_STATUS_TONE } from './projectsFormat';

/** Pull a human string out of any thrown value — never "[object Object]". */
function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** A concise, stable label for a product in the picker (mirrors POForm's). */
function productLabel(p: PickerProduct): string {
  return `${p.code ? `${p.code} — ` : ''}${p.name}${p.brand ? ` (${p.brand})` : ''}`;
}

/** Debounce a rapidly-changing value (the product-search query) by `delay` ms. */
function useDebounced<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

const PROJECTS_BASE = '/m/projects';

/** A label/value pair on the header summary grid (mirrors PODetail's DefItem). */
function DefItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-800">{children}</dd>
    </div>
  );
}

/** A displayable PO title for the table — the number, or a placeholder when unset. */
function poNumberLabel(poNumber: string | null): string {
  return poNumber ? poNumber : '— (no number)';
}

/** Integer paise → a rupee string, or an em-dash when null (a template field left unset). */
function rupeesOrDash(paise: number | null): string {
  return paise == null ? '—' : rupees(paise);
}

/** Integer paise → a rupee INPUT string ("123.45"), blank when null. */
function paiseToRupeeInput(paise: number | null): string {
  return paise == null ? '' : (paise / 100).toFixed(2);
}

/**
 * Project detail — the project's header (code / name / client / status / dates /
 * description) plus a drill-through table of its purchase orders. Reached from the
 * Projects register; reads need projects VIEW (server-enforced). Each PO row links
 * out to the PO detail in the Sales Orders module.
 */
export function ProjectDetail() {
  const params = useParams();
  const id = params.id ?? null;

  const projectQuery = useProjectQuery(id);

  const backLink = (
    <Link to={PROJECTS_BASE} className="text-sm font-medium text-brand-600 hover:text-brand-700">
      ← Back to projects
    </Link>
  );

  if (id == null) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <StatePanel tone="red" title="Invalid project">
          That project link is not valid.
        </StatePanel>
      </div>
    );
  }

  if (projectQuery.isPending) return <Loading label="Loading project…" />;
  if (projectQuery.isError || !projectQuery.data) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <ErrorState error={projectQuery.error} onRetry={() => void projectQuery.refetch()} />
      </div>
    );
  }

  const project = projectQuery.data;

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={project.code}
        subtitle={project.name}
        actions={
          <Badge tone={PROJECT_STATUS_TONE[project.status]}>
            {PROJECT_STATUS_LABEL[project.status]}
          </Badge>
        }
      />

      <div className="mb-6 rounded-xl border border-slate-200 bg-white p-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <DefItem label="Status">
            <Badge tone={PROJECT_STATUS_TONE[project.status]}>
              {PROJECT_STATUS_LABEL[project.status]}
            </Badge>
          </DefItem>
          <DefItem label="Client">
            <span className="font-mono text-slate-900">{project.client_code}</span>
            <span className="text-slate-400"> — </span>
            <span>{project.client_name}</span>
          </DefItem>
          <DefItem label="Start date">{formatDate(project.start_date)}</DefItem>
          <DefItem label="Created">{formatDate(project.created_at)}</DefItem>
        </dl>
        {project.description && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">
              Description
            </dt>
            <dd className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700">
              {project.description}
            </dd>
          </div>
        )}
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Purchase orders</h2>
      <PurchaseOrdersSection projectId={id} />

      <ProductsSection projectId={id} />
    </div>
  );
}

/** Backend's max page size — a per-project drill-through requests the whole bounded set
 * (a project's POs), not the register's default first page, so nothing is silently hidden. */
const PROJECT_POS_LIMIT = 200;

/** The project's PO drill-through: the register columns, scoped to this project. */
function PurchaseOrdersSection({ projectId }: { projectId: string }) {
  const query = usePurchaseOrdersQuery({ project_id: projectId, limit: PROJECT_POS_LIMIT });

  if (query.isPending) return <Loading label="Loading purchase orders…" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <StatePanel title="No purchase orders">No purchase orders for this project yet.</StatePanel>;
  }

  return (
    <>
      <Table>
        <THead>
          <Tr>
            <Th>PO number</Th>
            <Th>PO date</Th>
            <Th>Status</Th>
            <Th className="text-right">Lines</Th>
            <Th className="text-right">Order value</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </THead>
        <tbody>
          {rows.map((po) => (
            <Tr key={po.id}>
              <Td className="font-medium text-slate-900">{poNumberLabel(po.po_number)}</Td>
              <Td className="whitespace-nowrap">{formatDate(po.po_date)}</Td>
              <Td>
                <Badge tone={PO_STATUS_TONE[po.status as POStatus]}>
                  {PO_STATUS_LABEL[po.status as POStatus] ?? po.status}
                </Badge>
              </Td>
              <Td className="text-right tabular-nums">{po.line_count}</Td>
              {/* Client-facing order value (entire client billing — goods + freight +
                  packaging/handling/other — plus agency fee). Visible to all; no admin margin. */}
              <Td className="text-right tabular-nums">{rupees(po.total_with_agency_paise)}</Td>
              <Td>
                <div className="flex justify-end">
                  <Link
                    to={`${SALES_ORDERS_BASE}/${po.id}`}
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
      <p className="mt-4 text-sm text-slate-500">
        Showing {rows.length}
        {rows.length >= PROJECT_POS_LIMIT && ` (first ${PROJECT_POS_LIMIT} — narrow via the register)`}
      </p>
    </>
  );
}

/**
 * The project's tagged-products curation + per-project PRICING TEMPLATE. Reads need
 * `sales_orders` VIEW (the whole section is HIDDEN, not 403'd, for a user without it);
 * tagging/removing/editing pricing need OPERATE (server-enforced `product.tag`). The
 * tagged list includes inactive products, badged accordingly. The Add picker searches
 * the FULL catalogue (no project scope) so an operator can tag any product, tagging it
 * with no pricing (edit it afterwards). "Edit" opens the pricing template modal.
 */
function ProductsSection({ projectId }: { projectId: string }) {
  const perms = usePermissions();
  const canView = perms.canAccessModule('sales_orders');
  const canOperate = perms.atLeast('sales_orders', 'OPERATE');
  // Only admins (platform IAM) may view/enter the "actual" sell + freight — the SAME
  // gate POForm uses; the backend masks those two fields for everyone else.
  const isAdmin = perms.hasPlatform('iam');
  const toast = useToast();

  // Disabled (never fetched) when the user lacks VIEW, so we hide rather than 403.
  const query = useProjectProductsQuery(canView ? projectId : null);
  const tag = useTagProduct(projectId);
  const untag = useUntagProduct(projectId);
  const update = useUpdateProjectProduct(projectId);
  // Which product row's Remove is in flight — so only that button shows a spinner and the
  // others stay usable (a single shared pending flag would disable every row at once).
  const [removingId, setRemovingId] = useState<string | null>(null);
  // The row whose pricing template is being edited (null = modal closed).
  const [editing, setEditing] = useState<ProjectProduct | null>(null);

  if (!canView) return null;

  // The ids already tagged — excluded from the Add picker so a product can't be
  // double-added. product_id is numeric on the wire; the picker's ids are strings.
  const taggedIds = new Set((query.data ?? []).map((p) => String(p.product_id)));

  const handleTag = (productId: string) => {
    if (!productId) return;
    tag.mutate(productId, {
      onSuccess: (p) => toast.success(`Tagged ${p.name}.`),
      onError: (err) => toast.error(errorMessage(err)),
    });
  };
  const handleUntag = (product: ProjectProduct) => {
    const pid = String(product.product_id);
    setRemovingId(pid);
    untag.mutate(pid, {
      onSuccess: () => toast.success(`Removed ${product.name}.`),
      onError: (err) => toast.error(errorMessage(err)),
      onSettled: () => setRemovingId(null),
    });
  };
  const handleSavePricing = (body: ProjectProductPricing) => {
    if (!editing) return;
    // Nothing entered/changed → don't fire a no-op PATCH or a misleading "saved" toast.
    if (Object.keys(body).length === 0) {
      setEditing(null);
      return;
    }
    update.mutate(
      { productId: String(editing.product_id), body },
      {
        onSuccess: () => {
          setEditing(null);
          toast.success('Pricing template saved.');
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  };

  return (
    <section className="mt-8">
      <h2 className="mb-2 text-sm font-semibold text-slate-900">Tagged products</h2>

      {canOperate && (
        <div className="mb-3 max-w-md">
          <AddProductPicker onSelect={handleTag} disabled={tag.isPending} excludeIds={taggedIds} />
        </div>
      )}

      <ProductsTable
        query={query}
        canOperate={canOperate}
        onEdit={setEditing}
        onRemove={handleUntag}
        removingId={removingId}
      />

      <PricingFormModal
        open={editing != null}
        product={editing}
        isAdmin={isAdmin}
        saving={update.isPending}
        onSubmit={handleSavePricing}
        onClose={() => setEditing(null)}
      />
    </section>
  );
}

/** The tagged-products pricing editor table (Code / Name / Client sell / Our CP / Tax %),
 * with loading / error / empty states mirroring the Purchase orders section. */
function ProductsTable({
  query,
  canOperate,
  onEdit,
  onRemove,
  removingId,
}: {
  query: ReturnType<typeof useProjectProductsQuery>;
  canOperate: boolean;
  onEdit: (product: ProjectProduct) => void;
  onRemove: (product: ProjectProduct) => void;
  removingId: string | null;
}) {
  if (query.isPending) return <Loading label="Loading products…" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return (
      <StatePanel title="No products">
        {canOperate
          ? 'No products tagged to this project yet — use “Add product” above to tag one.'
          : 'No products tagged to this project yet.'}
      </StatePanel>
    );
  }

  return (
    <Table>
      <THead>
        <Tr>
          <Th>Code</Th>
          <Th>Name</Th>
          <Th className="text-right">Client sell</Th>
          <Th className="text-right">Our CP</Th>
          <Th className="text-right">Tax %</Th>
          {canOperate && <Th className="text-right">Actions</Th>}
        </Tr>
      </THead>
      <tbody>
        {rows.map((p) => {
          const pid = String(p.product_id);
          return (
            <Tr key={pid}>
              <Td className="font-mono text-slate-700">{p.code ?? '—'}</Td>
              <Td className="text-slate-900">
                <span className="font-medium">{p.name}</span>
                {p.active === false && (
                  <span className="ml-2">
                    <Badge tone="slate">Inactive</Badge>
                  </span>
                )}
              </Td>
              <Td className="text-right tabular-nums">{rupeesOrDash(p.client_sell_price_paise)}</Td>
              <Td className="text-right tabular-nums">{rupeesOrDash(p.cost_price_paise)}</Td>
              <Td className="text-right tabular-nums">{p.tax_rate != null ? `${p.tax_rate}%` : '—'}</Td>
              {canOperate && (
                <Td>
                  <div className="flex items-center justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Edit ${p.name}`}
                      onClick={() => onEdit(p)}
                    >
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Remove ${p.name}`}
                      loading={removingId === pid}
                      disabled={removingId === pid}
                      onClick={() => onRemove(p)}
                      className="text-rose-600 hover:bg-rose-50"
                    >
                      Remove
                    </Button>
                  </div>
                </Td>
              )}
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

// --- pricing template editor --------------------------------------------------

/** The editable pricing-template form values. Every input is a string; money is in ₹. */
interface PricingFormValues {
  cost: string;
  originalCost: string;
  clientSell: string;
  vendorSell: string;
  actualSell: string; // admin-only
  clientFreight: string;
  vendorFreight: string;
  actualFreight: string; // admin-only
  packaging: string;
  handling: string;
  other: string;
  taxRate: string;
  description: string;
  uom: string;
}

/** The integer-paise money keys on the wire that a form field maps to. */
type PricingMoneyKey =
  | 'cost_price_paise'
  | 'original_cost_price_paise'
  | 'client_sell_price_paise'
  | 'vendor_sell_price_paise'
  | 'sell_price_paise'
  | 'client_freight_paise'
  | 'vendor_freight_paise'
  | 'freight_paise'
  | 'packaging_paise'
  | 'handling_paise'
  | 'other_paise';

interface MoneyFieldDef {
  key: keyof PricingFormValues;
  paiseKey: PricingMoneyKey;
  label: string;
}

/** Money fields visible to everyone (paise on the wire). */
const PRICING_MONEY_FIELDS: MoneyFieldDef[] = [
  { key: 'cost', paiseKey: 'cost_price_paise', label: 'Our CP (₹)' },
  { key: 'originalCost', paiseKey: 'original_cost_price_paise', label: 'Original CP (₹)' },
  { key: 'clientSell', paiseKey: 'client_sell_price_paise', label: 'Client sell (₹)' },
  { key: 'vendorSell', paiseKey: 'vendor_sell_price_paise', label: 'Vendor sell (₹)' },
  { key: 'clientFreight', paiseKey: 'client_freight_paise', label: 'Client freight (₹)' },
  { key: 'vendorFreight', paiseKey: 'vendor_freight_paise', label: 'Vendor freight (₹)' },
  { key: 'packaging', paiseKey: 'packaging_paise', label: 'Packaging (₹)' },
  { key: 'handling', paiseKey: 'handling_paise', label: 'Handling (₹)' },
  { key: 'other', paiseKey: 'other_paise', label: 'Other (₹)' },
];

/** Admin-only money fields — the ACTUALS the backend masks/ignores for non-admins. */
const PRICING_ADMIN_MONEY_FIELDS: MoneyFieldDef[] = [
  { key: 'actualSell', paiseKey: 'sell_price_paise', label: '🔒 Actual sell (₹) (admin only)' },
  { key: 'actualFreight', paiseKey: 'freight_paise', label: '🔒 Actual freight (₹) (admin only)' },
];

const EMPTY_PRICING_FORM: PricingFormValues = {
  cost: '',
  originalCost: '',
  clientSell: '',
  vendorSell: '',
  actualSell: '',
  clientFreight: '',
  vendorFreight: '',
  actualFreight: '',
  packaging: '',
  handling: '',
  other: '',
  taxRate: '',
  description: '',
  uom: '',
};

/** Pre-fill the form from a tagged product's template (paise→₹ for money; strings verbatim). */
function productToPricingForm(p: ProjectProduct): PricingFormValues {
  return {
    cost: paiseToRupeeInput(p.cost_price_paise),
    originalCost: paiseToRupeeInput(p.original_cost_price_paise),
    clientSell: paiseToRupeeInput(p.client_sell_price_paise),
    vendorSell: paiseToRupeeInput(p.vendor_sell_price_paise),
    actualSell: paiseToRupeeInput(p.sell_price_paise),
    clientFreight: paiseToRupeeInput(p.client_freight_paise),
    vendorFreight: paiseToRupeeInput(p.vendor_freight_paise),
    actualFreight: paiseToRupeeInput(p.freight_paise),
    packaging: paiseToRupeeInput(p.packaging_paise),
    handling: paiseToRupeeInput(p.handling_paise),
    other: paiseToRupeeInput(p.other_paise),
    taxRate: p.tax_rate ?? '',
    description: p.description ?? '',
    uom: p.uom ?? '',
  };
}

/** An optional money cell: blank is fine (→ omitted), otherwise it must parse to paise. */
function optionalMoneyValid(text: string): boolean {
  return text.trim() === '' || rupeesToPaise(text) != null;
}

/** An optional tax-rate cell: blank is fine (→ omitted), otherwise 0..100. */
function taxRateValid(text: string): boolean {
  if (text.trim() === '') return true;
  const n = Number(text.trim());
  return Number.isFinite(n) && n >= 0 && n <= 100;
}

/**
 * Build the partial pricing body from the form: every money field converted ₹→paise
 * (admin-only actuals ONLY when `isAdmin`), tax as a trimmed string, description/uom as
 * trimmed strings — each included ONLY when the operator left a non-empty value (blanks
 * are omitted, so a template stays partial). Assumes the caller has blocked submit on an
 * invalid money/tax field.
 */
function pricingFormToBody(v: PricingFormValues, isAdmin: boolean): ProjectProductPricing {
  const body: ProjectProductPricing = {};
  const money = isAdmin ? [...PRICING_MONEY_FIELDS, ...PRICING_ADMIN_MONEY_FIELDS] : PRICING_MONEY_FIELDS;
  for (const { key, paiseKey } of money) {
    const raw = v[key].trim();
    if (raw === '') continue;
    const paise = rupeesToPaise(raw);
    if (paise != null) body[paiseKey] = paise;
  }
  if (v.taxRate.trim()) body.tax_rate = v.taxRate.trim();
  if (v.description.trim()) body.description = v.description.trim();
  if (v.uom.trim()) body.uom = v.uom.trim();
  return body;
}

/**
 * Modal to edit a tagged product's per-project pricing template. Money inputs are RUPEES,
 * validated with rupeesToPaise (an inline error on an unparseable amount); the admin-only
 * Actual sell/freight inputs render ONLY for an admin. On save, only non-empty fields are
 * PATCHed (₹→paise), so a template can be saved partially.
 */
function PricingFormModal({
  open,
  product,
  isAdmin,
  saving,
  onSubmit,
  onClose,
}: {
  open: boolean;
  product: ProjectProduct | null;
  isAdmin: boolean;
  saving: boolean;
  onSubmit: (body: ProjectProductPricing) => void;
  onClose: () => void;
}) {
  const [values, setValues] = useState<PricingFormValues>(EMPTY_PRICING_FORM);
  // Re-seed the form each time the modal opens (from the row's current template).
  const [seeded, setSeeded] = useState(false);
  if (open && !seeded) {
    setValues(product ? productToPricingForm(product) : EMPTY_PRICING_FORM);
    setSeeded(true);
  }
  if (!open && seeded) setSeeded(false);

  function set(key: keyof PricingFormValues, value: string) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  const moneyDefs = isAdmin
    ? [...PRICING_MONEY_FIELDS, ...PRICING_ADMIN_MONEY_FIELDS]
    : PRICING_MONEY_FIELDS;
  const moneyErrors = new Set(
    moneyDefs.filter(({ key }) => !optionalMoneyValid(values[key])).map(({ key }) => key),
  );
  const taxErr = !taxRateValid(values.taxRate);
  const canSubmit = moneyErrors.size === 0 && !taxErr && !saving;

  function submit() {
    if (!canSubmit) return;
    onSubmit(pricingFormToBody(values, isAdmin));
  }

  const moneyError = 'Enter a valid rupee amount (numbers, up to 2 decimals).';

  return (
    <Modal
      open={open}
      title={product ? `Pricing — ${product.name}` : 'Pricing'}
      onClose={onClose}
      busy={saving}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSubmit} loading={saving}>
            Save pricing
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="space-y-3"
      >
        <p className="text-xs text-slate-500">
          Every field is optional — set what you know, leave the rest blank. This template
          pre-fills a New PO line for this project.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {moneyDefs.map(({ key, label }) => (
            <TextField
              key={key}
              label={label}
              inputMode="decimal"
              placeholder="0.00"
              value={values[key]}
              onChange={(e) => set(key, e.target.value)}
              error={moneyErrors.has(key) ? moneyError : undefined}
            />
          ))}
          <TextField
            label="Tax rate (%)"
            inputMode="decimal"
            value={values.taxRate}
            onChange={(e) => set('taxRate', e.target.value)}
            error={taxErr ? 'Enter a tax rate from 0 to 100.' : undefined}
          />
          <TextField
            label="Unit (UOM)"
            maxLength={20}
            placeholder="e.g. PCS"
            value={values.uom}
            onChange={(e) => set('uom', e.target.value)}
          />
        </div>
        <TextField
          label="Description"
          maxLength={500}
          value={values.description}
          onChange={(e) => set('description', e.target.value)}
        />
      </form>
    </Modal>
  );
}

/** The "Add product" combobox — SERVER-searched over the FULL catalogue (no project
 * scope) so any product can be tagged. Selecting a product tags it; the picker's own
 * value stays cleared so it reads as an "add" action. */
function AddProductPicker({
  onSelect,
  disabled,
  excludeIds,
}: {
  onSelect: (productId: string) => void;
  disabled?: boolean;
  /** Product ids already tagged — filtered out so a product can't be double-added. */
  excludeIds: Set<string>;
}) {
  const [queryText, setQueryText] = useState('');
  const debounced = useDebounced(queryText, 250);
  // No projectId → full catalogue (the operator may tag ANY product).
  const search = useProductSearch(debounced);
  const products = useMemo(() => search.data ?? [], [search.data]);

  const options = useMemo<SearchableSelectOption[]>(
    () =>
      products
        .filter((p) => !excludeIds.has(p.id))
        .map((p) => ({ value: p.id, label: productLabel(p) })),
    [products, excludeIds],
  );

  return (
    <SearchableSelect
      label="Add product"
      value={null}
      onChange={onSelect}
      onQueryChange={setQueryText}
      options={options}
      disabled={disabled}
      placeholder="Search products to tag…"
      hint={search.isFetching ? 'Searching…' : 'Tag any catalogue product to this project.'}
    />
  );
}
