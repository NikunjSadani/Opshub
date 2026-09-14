import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  PageHeader,
  SearchableSelect,
  StatePanel,
  Table,
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
  useProductSearch,
  usePurchaseOrdersQuery,
  type PickerProduct,
  type POStatus,
} from '../../api/purchaseOrders';
import {
  useProjectProductsQuery,
  useTagProduct,
  useUntagProduct,
} from '../../api/projectProducts';
import type { Product } from '../../api/products';
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
 * The project's tagged-products curation. Reads need `sales_orders` VIEW (the whole
 * section is HIDDEN, not 403'd, for a user without it); tagging/removing need OPERATE
 * (server-enforced `product.tag`). The tagged list includes inactive products, badged
 * accordingly. The Add picker searches the FULL catalogue (no project scope) so an
 * operator can tag any product.
 */
function ProductsSection({ projectId }: { projectId: string }) {
  const perms = usePermissions();
  const canView = perms.canAccessModule('sales_orders');
  const canOperate = perms.atLeast('sales_orders', 'OPERATE');
  const toast = useToast();

  // Disabled (never fetched) when the user lacks VIEW, so we hide rather than 403.
  const query = useProjectProductsQuery(canView ? projectId : null);
  const tag = useTagProduct(projectId);
  const untag = useUntagProduct(projectId);
  // Which product row's Remove is in flight — so only that button shows a spinner and the
  // others stay usable (a single shared pending flag would disable every row at once).
  const [removingId, setRemovingId] = useState<string | null>(null);

  if (!canView) return null;

  // The ids already tagged — excluded from the Add picker so a product can't be double-added.
  const taggedIds = new Set((query.data ?? []).map((p) => p.id));

  const handleTag = (productId: string) => {
    if (!productId) return;
    tag.mutate(productId, {
      onSuccess: (p) => toast.success(`Tagged ${p.name}.`),
      onError: (err) => toast.error(errorMessage(err)),
    });
  };
  const handleUntag = (product: Product) => {
    setRemovingId(product.id);
    untag.mutate(product.id, {
      onSuccess: () => toast.success(`Removed ${product.name}.`),
      onError: (err) => toast.error(errorMessage(err)),
      onSettled: () => setRemovingId(null),
    });
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
        onRemove={handleUntag}
        removingId={removingId}
      />
    </section>
  );
}

/** The tagged-products table (Code / Name / Brand / Category / UOM), with loading /
 * error / empty states mirroring the Purchase orders section. */
function ProductsTable({
  query,
  canOperate,
  onRemove,
  removingId,
}: {
  query: ReturnType<typeof useProjectProductsQuery>;
  canOperate: boolean;
  onRemove: (product: Product) => void;
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
          <Th>Brand</Th>
          <Th>Category</Th>
          <Th>UOM</Th>
          {canOperate && <Th className="text-right">Actions</Th>}
        </Tr>
      </THead>
      <tbody>
        {rows.map((p) => (
          <Tr key={p.id}>
            <Td className="font-mono text-slate-700">{p.code ?? '—'}</Td>
            <Td className="text-slate-900">
              <span className="font-medium">{p.name}</span>
              {p.active === false && (
                <span className="ml-2">
                  <Badge tone="slate">Inactive</Badge>
                </span>
              )}
            </Td>
            <Td>{p.brand ?? '—'}</Td>
            <Td>{p.category ?? '—'}</Td>
            <Td>{p.uom}</Td>
            {canOperate && (
              <Td>
                <div className="flex justify-end">
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove ${p.name}`}
                    loading={removingId === p.id}
                    disabled={removingId === p.id}
                    onClick={() => onRemove(p)}
                    className="text-rose-600 hover:bg-rose-50"
                  >
                    Remove
                  </Button>
                </div>
              </Td>
            )}
          </Tr>
        ))}
      </tbody>
    </Table>
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
