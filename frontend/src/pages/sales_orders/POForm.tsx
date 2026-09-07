import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Button,
  ErrorState,
  PageHeader,
  SearchableSelect,
  SelectField,
  TextArea,
  TextField,
  useToast,
  type SearchableSelectOption,
} from '../../ui';
import { ApiError, useApi } from '../../api/client';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery, useProjectsQuery } from '../../api/projects';
import {
  AGENCY_FEE_LABEL,
  rupeesToPaise,
  useClientGstinsQuery,
  useCreatePurchaseOrder,
  useProductSearch,
  type AgencyFeeType,
  type PickerProduct,
  type POCreateInput,
  type POLineInput,
} from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** A concise, stable label for a product in the picker. */
function productLabel(p: PickerProduct): string {
  return `${p.code ? `${p.code} — ` : ''}${p.name}${p.brand ? ` (${p.brand})` : ''}`;
}

/** Debounce a rapidly-changing value (e.g. the product search query) by `delay` ms. */
function useDebounced<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

/** One editable line row (money is kept as the operator's rupee text until submit). */
interface LineRow {
  key: number;
  productId: string;
  description: string;
  uom: string;
  qty: string;
  // Cost
  originalCost: string;
  cost: string; // Our CP (billed) — required
  // Sell
  clientSell: string; // required
  vendorSell: string;
  actualSell: string; // admin-only
  // Freight
  clientFreight: string;
  vendorFreight: string;
  actualFreight: string; // admin-only
  // Other charges
  packaging: string;
  handling: string;
  other: string;
  taxRate: string;
}

let nextKey = 1;
function blankLine(): LineRow {
  return {
    key: nextKey++,
    productId: '',
    description: '',
    uom: '',
    qty: '',
    originalCost: '',
    cost: '',
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
  };
}

/** A positive quantity? (accepts decimals). */
function qtyValid(qty: string): boolean {
  const n = Number(qty.trim());
  return Number.isFinite(n) && n > 0;
}

/** An optional money cell: blank is fine (→ omitted), otherwise it must parse to paise. */
function optionalMoneyValid(text: string): boolean {
  return text.trim() === '' || rupeesToPaise(text) != null;
}

/** An optional tax-rate cell: blank is fine (→ 0), otherwise 0..100. */
function taxValid(text: string): boolean {
  if (text.trim() === '') return true;
  const n = Number(text.trim());
  return Number.isFinite(n) && n >= 0 && n <= 100;
}

/** A percent 0..100 (used for the agency fee). */
function percentValid(text: string): boolean {
  const n = Number(text.trim());
  return text.trim() !== '' && Number.isFinite(n) && n >= 0 && n <= 100;
}

function lineValid(row: LineRow): boolean {
  return (
    !!row.productId &&
    qtyValid(row.qty) &&
    rupeesToPaise(row.cost) != null &&
    rupeesToPaise(row.clientSell) != null &&
    optionalMoneyValid(row.originalCost) &&
    optionalMoneyValid(row.vendorSell) &&
    optionalMoneyValid(row.actualSell) &&
    optionalMoneyValid(row.clientFreight) &&
    optionalMoneyValid(row.vendorFreight) &&
    optionalMoneyValid(row.actualFreight) &&
    optionalMoneyValid(row.packaging) &&
    optionalMoneyValid(row.handling) &&
    optionalMoneyValid(row.other) &&
    taxValid(row.taxRate)
  );
}

/** Our CP = Original CP + (Vendor SP − Original CP) / 2 (computed in paise, returned as ₹). */
function computeOurCp(originalCost: string, vendorSell: string): string | null {
  const o = rupeesToPaise(originalCost);
  const v = rupeesToPaise(vendorSell);
  if (o == null || v == null) return null;
  const cp = o + Math.round((v - o) / 2);
  return (cp / 100).toFixed(2);
}

/** Optional-money → paise, or undefined when the cell is blank. */
function optMoney(text: string): number | undefined {
  return text.trim() ? (rupeesToPaise(text) as number) : undefined;
}

/** The Purchase Order create form (OPERATE). */
export function POForm() {
  const toast = useToast();
  const navigate = useNavigate();
  const perms = usePermissions();
  // Only admins (platform IAM) may view/enter the "actual" sell + freight — matches
  // the backend has_platform(IAM) gate that masks those fields for everyone else.
  const isAdmin = perms.hasPlatform('iam');

  const [poNumber, setPoNumber] = useState('');
  const [clientId, setClientId] = useState('');
  const [gstinId, setGstinId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [poDate, setPoDate] = useState('');
  const [expectedDate, setExpectedDate] = useState('');
  const [notes, setNotes] = useState('');
  const [lines, setLines] = useState<LineRow[]>([blankLine()]);

  // Agency fee (PO header). Value input is conditional on the type.
  const [agencyFeeType, setAgencyFeeType] = useState<AgencyFeeType>('NONE');
  const [agencyFeePercent, setAgencyFeePercent] = useState('');
  const [agencyFeeAmount, setAgencyFeeAmount] = useState('');

  // Optional soft-copy attachment: uploaded up-front to `/files/upload`, then its id
  // is sent as `soft_copy_file_id` on create. Upload needs OPERATE (which PO create
  // already requires), so no extra gate here.
  const [softCopy, setSoftCopy] = useState<{ id: string; filename: string } | null>(null);
  const [uploadingSoftCopy, setUploadingSoftCopy] = useState(false);

  const { postForm } = useApi();
  const clientsQuery = useClientsQuery();
  // Projects filtered by the chosen client; only ACTIVE ones are choosable (the
  // backend rejects a non-active project). Reset project/GSTIN when the client changes.
  const projectsQuery = useProjectsQuery({ client_id: clientId, status: 'ACTIVE' });
  const gstinsQuery = useClientGstinsQuery(clientId || null);
  const create = useCreatePurchaseOrder();

  // SERVER-searched product picker: one debounced query drives one fetch, and the
  // options are shared by every line's combobox. `selectedProducts` remembers the
  // products already chosen so their labels stay stable even when they drop out of
  // the latest result page.
  const [productQuery, setProductQuery] = useState('');
  const debouncedProductQuery = useDebounced(productQuery, 250);
  const productsQuery = useProductSearch(debouncedProductQuery);
  const products = useMemo(() => productsQuery.data ?? [], [productsQuery.data]);
  const [selectedProducts, setSelectedProducts] = useState<Record<string, PickerProduct>>({});

  const productOptions = useMemo<SearchableSelectOption[]>(() => {
    const map = new Map<string, string>();
    for (const p of products) map.set(p.id, productLabel(p));
    // Preserve any selected-but-missing product's label so the picker stays stable.
    for (const [id, p] of Object.entries(selectedProducts)) {
      if (!map.has(id)) map.set(id, productLabel(p));
    }
    return Array.from(map, ([value, label]) => ({ value, label }));
  }, [products, selectedProducts]);

  function updateLine(key: number, patch: Partial<LineRow>) {
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }
  function selectProduct(key: number, id: string) {
    const found = products.find((p) => p.id === id);
    if (found) setSelectedProducts((prev) => ({ ...prev, [id]: found }));
    updateLine(key, { productId: id });
  }
  function addLine() {
    setLines((prev) => [...prev, blankLine()]);
  }
  function removeLine(key: number) {
    setLines((prev) => (prev.length === 1 ? prev : prev.filter((l) => l.key !== key)));
  }
  function computeLineCp(key: number) {
    const row = lines.find((l) => l.key === key);
    if (!row) return;
    const cp = computeOurCp(row.originalCost, row.vendorSell);
    if (cp != null) updateLine(key, { cost: cp });
  }

  function onClientChange(id: string) {
    setClientId(id);
    setProjectId('');
    setGstinId('');
  }

  async function onSoftCopyChange(fileList: FileList | null) {
    const chosen = fileList?.[0];
    if (!chosen) return;
    setUploadingSoftCopy(true);
    try {
      const form = new FormData();
      form.append('file', chosen);
      form.append('module_key', 'sales_orders');
      const out = await postForm<{ id: number | string; filename: string; size: number }>(
        '/files/upload',
        form,
      );
      setSoftCopy({ id: String(out.id), filename: out.filename });
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setUploadingSoftCopy(false);
    }
  }

  const agencyFeeValid =
    agencyFeeType === 'NONE' ||
    (agencyFeeType === 'PERCENT' && percentValid(agencyFeePercent)) ||
    (agencyFeeType === 'FIXED' && rupeesToPaise(agencyFeeAmount) != null);

  const linesValid = lines.every(lineValid);
  // NB: po_number is now OPTIONAL — it can be added later on the detail page.
  const canSubmit =
    !!clientId &&
    !!projectId &&
    !!poDate &&
    lines.length > 0 &&
    linesValid &&
    agencyFeeValid &&
    !uploadingSoftCopy;

  function onSubmit() {
    if (!canSubmit) return;
    const payloadLines: POLineInput[] = lines.map((l) => ({
      product_id: l.productId,
      description: l.description.trim() || undefined,
      uom: l.uom.trim() || undefined,
      ordered_qty: l.qty.trim(),
      // Non-null: canSubmit already proved every required money cell parses.
      cost_price_paise: rupeesToPaise(l.cost) as number,
      original_cost_price_paise: optMoney(l.originalCost),
      client_sell_price_paise: rupeesToPaise(l.clientSell) as number,
      vendor_sell_price_paise: optMoney(l.vendorSell),
      // Actual sell/freight are admin-only: never send them for a non-admin (the
      // inputs aren't even rendered), and only when the operator actually filled them.
      sell_price_paise: isAdmin ? optMoney(l.actualSell) : undefined,
      client_freight_paise: optMoney(l.clientFreight),
      vendor_freight_paise: optMoney(l.vendorFreight),
      freight_paise: isAdmin ? optMoney(l.actualFreight) : undefined,
      // Packaging / Handling / Other kept as-is: blank → 0 (as the prior form sent them).
      packaging_paise: l.packaging.trim() ? (rupeesToPaise(l.packaging) as number) : 0,
      handling_paise: l.handling.trim() ? (rupeesToPaise(l.handling) as number) : 0,
      other_paise: l.other.trim() ? (rupeesToPaise(l.other) as number) : 0,
      tax_rate: l.taxRate.trim() ? Number(l.taxRate.trim()) : 0,
    }));

    const body: POCreateInput = {
      po_number: poNumber.trim() || undefined,
      client_id: clientId,
      client_gstin_id: gstinId || undefined,
      project_id: projectId,
      po_date: poDate,
      expected_procurement_date: expectedDate || undefined,
      notes: notes.trim() || undefined,
      soft_copy_file_id: softCopy?.id ?? undefined,
      agency_fee_type: agencyFeeType,
      agency_fee_percent:
        agencyFeeType === 'PERCENT' ? Number(agencyFeePercent.trim()) : undefined,
      agency_fee_amount_paise:
        agencyFeeType === 'FIXED' ? (rupeesToPaise(agencyFeeAmount) as number) : undefined,
      lines: payloadLines,
    };

    create.mutate(body, {
      onSuccess: (po) => {
        toast.success(`Purchase order ${po.po_number ?? '(no number)'} created.`);
        navigate(`${SALES_ORDERS_BASE}/${po.id}`);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  if (clientsQuery.isError) {
    return <ErrorState error={clientsQuery.error} onRetry={() => void clientsQuery.refetch()} />;
  }

  const projects = projectsQuery.data ?? [];
  const gstins = gstinsQuery.data ?? [];

  return (
    <div>
      <PageHeader
        title="New purchase order"
        subtitle="Create a PO by hand — pick the client, project, and add line items."
        actions={
          <Link
            to={SALES_ORDERS_BASE}
            className="text-sm font-medium text-slate-500 hover:text-slate-700"
          >
            Back to register
          </Link>
        }
      />

      <div className="max-w-4xl">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <TextField
            label="PO number (optional)"
            value={poNumber}
            onChange={(e) => setPoNumber(e.target.value)}
            maxLength={64}
            placeholder="e.g. PO-2026-001"
            hint="Optional — you can add it later from the PO page."
          />
          <SelectField
            label="Client"
            required
            value={clientId}
            onChange={(e) => onClientChange(e.target.value)}
            disabled={clientsQuery.isPending}
          >
            <option value="">
              {clientsQuery.isPending ? 'Loading clients…' : 'Select a client…'}
            </option>
            {(clientsQuery.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.code} — {c.name}
              </option>
            ))}
          </SelectField>
          <div>
            <SelectField
              label="Project"
              required
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              disabled={!clientId || projectsQuery.isPending || projectsQuery.isError}
              hint={!clientId ? 'Choose a client first.' : undefined}
              error={
                clientId && projectsQuery.isError ? "Couldn't load projects." : undefined
              }
            >
              <option value="">
                {!clientId
                  ? 'Select a client first…'
                  : projectsQuery.isPending
                    ? 'Loading projects…'
                    : projectsQuery.isError
                      ? 'Failed to load projects'
                      : projects.length === 0
                        ? 'No active projects for this client'
                        : 'Select a project…'}
              </option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.code} — {p.name}
                </option>
              ))}
            </SelectField>
            {clientId && projectsQuery.isError && (
              <button
                type="button"
                onClick={() => void projectsQuery.refetch()}
                className="mt-1 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                Retry
              </button>
            )}
          </div>
          <div>
            <SelectField
              label="Client GSTIN (optional)"
              value={gstinId}
              onChange={(e) => setGstinId(e.target.value)}
              disabled={!clientId || gstinsQuery.isPending || gstinsQuery.isError}
              error={clientId && gstinsQuery.isError ? "Couldn't load GSTINs." : undefined}
            >
              <option value="">
                {!clientId
                  ? 'Choose a client first…'
                  : gstinsQuery.isError
                    ? 'Failed to load GSTINs'
                    : 'No specific GSTIN'}
              </option>
              {gstins.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.gstin}
                  {g.legal_name ? ` — ${g.legal_name}` : ''}
                  {g.is_default ? ' (default)' : ''}
                </option>
              ))}
            </SelectField>
            {clientId && gstinsQuery.isError && (
              <button
                type="button"
                onClick={() => void gstinsQuery.refetch()}
                className="mt-1 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                Retry
              </button>
            )}
          </div>
          <TextField
            label="PO date"
            type="date"
            required
            value={poDate}
            onChange={(e) => setPoDate(e.target.value)}
          />
          <TextField
            label="Expected procurement date (optional)"
            type="date"
            value={expectedDate}
            onChange={(e) => setExpectedDate(e.target.value)}
            min={poDate || undefined}
          />
        </div>

        {/* Agency fee — header-level, charged on the whole order. */}
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <SelectField
            label="Agency fee"
            value={agencyFeeType}
            onChange={(e) => setAgencyFeeType(e.target.value as AgencyFeeType)}
          >
            {(Object.keys(AGENCY_FEE_LABEL) as AgencyFeeType[]).map((t) => (
              <option key={t} value={t}>
                {AGENCY_FEE_LABEL[t]}
              </option>
            ))}
          </SelectField>
          {agencyFeeType === 'PERCENT' && (
            <TextField
              label="Agency fee %"
              required
              inputMode="decimal"
              value={agencyFeePercent}
              onChange={(e) => setAgencyFeePercent(e.target.value)}
              error={
                agencyFeePercent.trim() !== '' && !percentValid(agencyFeePercent)
                  ? '0–100'
                  : undefined
              }
              placeholder="e.g. 2.5"
            />
          )}
          {agencyFeeType === 'FIXED' && (
            <TextField
              label="Agency fee ₹"
              required
              inputMode="decimal"
              value={agencyFeeAmount}
              onChange={(e) => setAgencyFeeAmount(e.target.value)}
              error={
                agencyFeeAmount.trim() !== '' && rupeesToPaise(agencyFeeAmount) == null
                  ? 'Invalid amount'
                  : undefined
              }
              placeholder="e.g. 5000"
            />
          )}
        </div>

        <div className="mt-3">
          <TextArea
            label="Notes (optional)"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            maxLength={1000}
          />
        </div>

        <div className="mt-3">
          <span className="mb-1 block text-xs font-medium text-slate-600">
            Soft copy (optional)
          </span>
          {softCopy ? (
            <div className="flex items-center gap-3 text-sm">
              <span className="font-medium text-slate-800">{softCopy.filename}</span>
              <button
                type="button"
                onClick={() => setSoftCopy(null)}
                className="text-xs font-medium text-rose-600 hover:text-rose-700"
              >
                Remove
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <input
                id="po-soft-copy"
                type="file"
                disabled={uploadingSoftCopy}
                onChange={(e) => void onSoftCopyChange(e.target.files)}
                className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100 disabled:opacity-50"
              />
              {uploadingSoftCopy && <span className="text-xs text-slate-500">Uploading…</span>}
            </div>
          )}
          <p className="mt-1 text-xs text-slate-400">
            Attach the signed PO / supporting document. Uploaded now; linked when the PO is
            created.
          </p>
        </div>

        <div className="mt-6 mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Line items</h2>
          <Button variant="secondary" size="sm" onClick={addLine} type="button">
            Add line
          </Button>
        </div>

        {productsQuery.isError ? (
          <ErrorState error={productsQuery.error} onRetry={() => void productsQuery.refetch()} />
        ) : (
          <div className="space-y-4">
            {lines.map((line, idx) => {
              const canCompute =
                rupeesToPaise(line.originalCost) != null && rupeesToPaise(line.vendorSell) != null;
              return (
                <div key={line.key} className="rounded-xl border border-slate-200 bg-white p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                      Line {idx + 1}
                    </span>
                    {lines.length > 1 && (
                      <button
                        type="button"
                        onClick={() => removeLine(line.key)}
                        className="text-xs font-medium text-rose-600 hover:text-rose-700"
                      >
                        Remove
                      </button>
                    )}
                  </div>

                  {/* Product + quantity/UOM/tax */}
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <div className="sm:col-span-2 lg:col-span-1">
                      <SearchableSelect
                        label="Product"
                        required
                        value={line.productId || null}
                        options={productOptions}
                        onChange={(id) => selectProduct(line.key, id)}
                        onQueryChange={setProductQuery}
                        placeholder="Search products…"
                        hint={productsQuery.isFetching ? 'Searching…' : undefined}
                      />
                    </div>
                    <TextField
                      label="Ordered qty"
                      required
                      inputMode="decimal"
                      value={line.qty}
                      onChange={(e) => updateLine(line.key, { qty: e.target.value })}
                      error={
                        line.qty.trim() !== '' && !qtyValid(line.qty) ? 'Must be > 0' : undefined
                      }
                    />
                    <TextField
                      label="UOM (optional)"
                      value={line.uom}
                      onChange={(e) => updateLine(line.key, { uom: e.target.value })}
                      maxLength={20}
                      placeholder="e.g. PCS"
                    />
                    <TextField
                      label="Tax rate % (optional)"
                      inputMode="decimal"
                      value={line.taxRate}
                      onChange={(e) => updateLine(line.key, { taxRate: e.target.value })}
                      error={!taxValid(line.taxRate) ? '0–100' : undefined}
                    />
                  </div>

                  {/* Cost / Sell / Freight groups */}
                  <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
                    {/* Cost */}
                    <fieldset className="rounded-lg border border-slate-200 p-2">
                      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                        Cost
                      </legend>
                      <div className="grid grid-cols-2 gap-2">
                        <TextField
                          label="Original CP ₹ (optional)"
                          inputMode="decimal"
                          value={line.originalCost}
                          onChange={(e) => updateLine(line.key, { originalCost: e.target.value })}
                          error={
                            !optionalMoneyValid(line.originalCost) ? 'Invalid amount' : undefined
                          }
                        />
                        <TextField
                          label="Our CP ₹"
                          required
                          inputMode="decimal"
                          value={line.cost}
                          onChange={(e) => updateLine(line.key, { cost: e.target.value })}
                          error={
                            line.cost.trim() !== '' && rupeesToPaise(line.cost) == null
                              ? 'Invalid amount'
                              : undefined
                          }
                        />
                      </div>
                      <button
                        type="button"
                        onClick={() => computeLineCp(line.key)}
                        disabled={!canCompute}
                        className="mt-2 text-xs font-medium text-brand-600 hover:text-brand-700 disabled:cursor-not-allowed disabled:text-slate-300"
                        title="Our CP = Original CP + (Vendor sell − Original CP) / 2"
                      >
                        Compute from 50%
                      </button>
                    </fieldset>

                    {/* Sell */}
                    <fieldset className="rounded-lg border border-slate-200 p-2">
                      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                        Sell
                      </legend>
                      <div className="grid grid-cols-2 gap-2">
                        <TextField
                          label="Client sell ₹"
                          required
                          inputMode="decimal"
                          value={line.clientSell}
                          onChange={(e) => updateLine(line.key, { clientSell: e.target.value })}
                          error={
                            line.clientSell.trim() !== '' && rupeesToPaise(line.clientSell) == null
                              ? 'Invalid amount'
                              : undefined
                          }
                        />
                        <TextField
                          label="Vendor sell ₹ (optional)"
                          inputMode="decimal"
                          value={line.vendorSell}
                          onChange={(e) => updateLine(line.key, { vendorSell: e.target.value })}
                          error={
                            !optionalMoneyValid(line.vendorSell) ? 'Invalid amount' : undefined
                          }
                        />
                        {isAdmin && (
                          <TextField
                            label="🔒 Actual sell ₹ (admin only)"
                            inputMode="decimal"
                            value={line.actualSell}
                            onChange={(e) => updateLine(line.key, { actualSell: e.target.value })}
                            error={
                              !optionalMoneyValid(line.actualSell) ? 'Invalid amount' : undefined
                            }
                          />
                        )}
                      </div>
                    </fieldset>

                    {/* Freight */}
                    <fieldset className="rounded-lg border border-slate-200 p-2">
                      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                        Freight
                      </legend>
                      <div className="grid grid-cols-2 gap-2">
                        <TextField
                          label="Client freight ₹ (optional)"
                          inputMode="decimal"
                          value={line.clientFreight}
                          onChange={(e) => updateLine(line.key, { clientFreight: e.target.value })}
                          error={
                            !optionalMoneyValid(line.clientFreight) ? 'Invalid amount' : undefined
                          }
                        />
                        <TextField
                          label="Vendor freight ₹ (optional)"
                          inputMode="decimal"
                          value={line.vendorFreight}
                          onChange={(e) => updateLine(line.key, { vendorFreight: e.target.value })}
                          error={
                            !optionalMoneyValid(line.vendorFreight) ? 'Invalid amount' : undefined
                          }
                        />
                        {isAdmin && (
                          <TextField
                            label="🔒 Actual freight ₹ (admin only)"
                            inputMode="decimal"
                            value={line.actualFreight}
                            onChange={(e) =>
                              updateLine(line.key, { actualFreight: e.target.value })
                            }
                            error={
                              !optionalMoneyValid(line.actualFreight) ? 'Invalid amount' : undefined
                            }
                          />
                        )}
                      </div>
                    </fieldset>
                  </div>

                  {/* Other charges + description */}
                  <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <TextField
                      label="Packaging ₹ (optional)"
                      inputMode="decimal"
                      value={line.packaging}
                      onChange={(e) => updateLine(line.key, { packaging: e.target.value })}
                      error={!optionalMoneyValid(line.packaging) ? 'Invalid amount' : undefined}
                    />
                    <TextField
                      label="Handling ₹ (optional)"
                      inputMode="decimal"
                      value={line.handling}
                      onChange={(e) => updateLine(line.key, { handling: e.target.value })}
                      error={!optionalMoneyValid(line.handling) ? 'Invalid amount' : undefined}
                    />
                    <TextField
                      label="Other ₹ (optional)"
                      inputMode="decimal"
                      value={line.other}
                      onChange={(e) => updateLine(line.key, { other: e.target.value })}
                      error={!optionalMoneyValid(line.other) ? 'Invalid amount' : undefined}
                    />
                    <TextField
                      label="Description (optional)"
                      value={line.description}
                      onChange={(e) => updateLine(line.key, { description: e.target.value })}
                      maxLength={500}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Button onClick={onSubmit} disabled={!canSubmit} loading={create.isPending}>
            Create purchase order
          </Button>
          {!canSubmit && !create.isPending && (
            <span className="text-xs text-slate-500">
              Fill the client, project, date, and at least one valid line (Our CP + Client sell).
              PO number is optional.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
