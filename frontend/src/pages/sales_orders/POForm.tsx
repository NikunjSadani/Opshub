import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Button,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  TextArea,
  TextField,
  useToast,
} from '../../ui';
import { ApiError } from '../../api/client';
import { useClientsQuery, useProjectsQuery } from '../../api/projects';
import {
  rupeesToPaise,
  useClientGstinsQuery,
  useCreatePurchaseOrder,
  useProductPicker,
  type POCreateInput,
  type POLineInput,
} from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** One editable line row (money is kept as the operator's rupee text until submit). */
interface LineRow {
  key: number;
  productId: string;
  description: string;
  uom: string;
  qty: string;
  cost: string;
  sell: string;
  freight: string;
  packaging: string;
  handling: string;
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
    cost: '',
    sell: '',
    freight: '',
    packaging: '',
    handling: '',
    taxRate: '',
  };
}

/** A positive quantity? (accepts decimals). */
function qtyValid(qty: string): boolean {
  const n = Number(qty.trim());
  return Number.isFinite(n) && n > 0;
}

/** An optional money cell: blank is fine (→ 0), otherwise it must parse to paise. */
function optionalMoneyValid(text: string): boolean {
  return text.trim() === '' || rupeesToPaise(text) != null;
}

/** An optional tax-rate cell: blank is fine (→ 0), otherwise 0..100. */
function taxValid(text: string): boolean {
  if (text.trim() === '') return true;
  const n = Number(text.trim());
  return Number.isFinite(n) && n >= 0 && n <= 100;
}

function lineValid(row: LineRow): boolean {
  return (
    !!row.productId &&
    qtyValid(row.qty) &&
    rupeesToPaise(row.cost) != null &&
    rupeesToPaise(row.sell) != null &&
    optionalMoneyValid(row.freight) &&
    optionalMoneyValid(row.packaging) &&
    optionalMoneyValid(row.handling) &&
    taxValid(row.taxRate)
  );
}

/** The Purchase Order create form (OPERATE). */
export function POForm() {
  const toast = useToast();
  const navigate = useNavigate();

  const [poNumber, setPoNumber] = useState('');
  const [clientId, setClientId] = useState('');
  const [gstinId, setGstinId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [poDate, setPoDate] = useState('');
  const [expectedDate, setExpectedDate] = useState('');
  const [notes, setNotes] = useState('');
  const [lines, setLines] = useState<LineRow[]>([blankLine()]);

  const clientsQuery = useClientsQuery();
  // Projects filtered by the chosen client; only ACTIVE ones are choosable (the
  // backend rejects a non-active project). Reset project/GSTIN when the client changes.
  const projectsQuery = useProjectsQuery({ client_id: clientId, status: 'ACTIVE' });
  const gstinsQuery = useClientGstinsQuery(clientId || null);
  const productsQuery = useProductPicker();
  const create = useCreatePurchaseOrder();

  const products = productsQuery.data ?? [];

  function updateLine(key: number, patch: Partial<LineRow>) {
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }
  function addLine() {
    setLines((prev) => [...prev, blankLine()]);
  }
  function removeLine(key: number) {
    setLines((prev) => (prev.length === 1 ? prev : prev.filter((l) => l.key !== key)));
  }

  function onClientChange(id: string) {
    setClientId(id);
    setProjectId('');
    setGstinId('');
  }

  const linesValid = lines.every(lineValid);
  const canSubmit =
    poNumber.trim() !== '' &&
    !!clientId &&
    !!projectId &&
    !!poDate &&
    lines.length > 0 &&
    linesValid;

  function onSubmit() {
    if (!canSubmit) return;
    const payloadLines: POLineInput[] = lines.map((l) => ({
      product_id: l.productId,
      description: l.description.trim() || undefined,
      uom: l.uom.trim() || undefined,
      ordered_qty: l.qty.trim(),
      // Non-null: canSubmit already proved every required money cell parses.
      cost_price_paise: rupeesToPaise(l.cost) as number,
      sell_price_paise: rupeesToPaise(l.sell) as number,
      freight_paise: l.freight.trim() ? (rupeesToPaise(l.freight) as number) : 0,
      packaging_paise: l.packaging.trim() ? (rupeesToPaise(l.packaging) as number) : 0,
      handling_paise: l.handling.trim() ? (rupeesToPaise(l.handling) as number) : 0,
      tax_rate: l.taxRate.trim() ? Number(l.taxRate.trim()) : 0,
    }));

    const body: POCreateInput = {
      po_number: poNumber.trim(),
      client_id: clientId,
      client_gstin_id: gstinId || undefined,
      project_id: projectId,
      po_date: poDate,
      expected_procurement_date: expectedDate || undefined,
      notes: notes.trim() || undefined,
      lines: payloadLines,
    };

    create.mutate(body, {
      onSuccess: (po) => {
        toast.success(`Purchase order ${po.po_number} created.`);
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
            label="PO number"
            required
            value={poNumber}
            onChange={(e) => setPoNumber(e.target.value)}
            maxLength={64}
            placeholder="e.g. PO-2026-001"
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
          <SelectField
            label="Project"
            required
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={!clientId || projectsQuery.isPending}
            hint={!clientId ? 'Choose a client first.' : undefined}
          >
            <option value="">
              {!clientId
                ? 'Select a client first…'
                : projectsQuery.isPending
                  ? 'Loading projects…'
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
          <SelectField
            label="Client GSTIN (optional)"
            value={gstinId}
            onChange={(e) => setGstinId(e.target.value)}
            disabled={!clientId || gstinsQuery.isPending}
          >
            <option value="">
              {!clientId ? 'Choose a client first…' : 'No specific GSTIN'}
            </option>
            {gstins.map((g) => (
              <option key={g.id} value={g.id}>
                {g.gstin}
                {g.legal_name ? ` — ${g.legal_name}` : ''}
                {g.is_default ? ' (default)' : ''}
              </option>
            ))}
          </SelectField>
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

        <div className="mt-3">
          <TextArea
            label="Notes (optional)"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            maxLength={1000}
          />
        </div>

        <div className="mt-6 mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Line items</h2>
          <Button variant="secondary" size="sm" onClick={addLine} type="button">
            Add line
          </Button>
        </div>

        {productsQuery.isError ? (
          <ErrorState error={productsQuery.error} onRetry={() => void productsQuery.refetch()} />
        ) : productsQuery.isPending ? (
          <Loading label="Loading products…" />
        ) : products.length === 0 ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No active products exist yet. Add products in the Products tab before creating a PO.
          </p>
        ) : (
          <div className="space-y-4">
            {lines.map((line, idx) => (
              <div
                key={line.key}
                className="rounded-xl border border-slate-200 bg-white p-3"
              >
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
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <SelectField
                    label="Product"
                    required
                    value={line.productId}
                    onChange={(e) => updateLine(line.key, { productId: e.target.value })}
                  >
                    <option value="">Select a product…</option>
                    {products.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.code ? `${p.code} — ` : ''}
                        {p.name}
                        {p.brand ? ` (${p.brand})` : ''}
                      </option>
                    ))}
                  </SelectField>
                  <TextField
                    label="Ordered qty"
                    required
                    inputMode="decimal"
                    value={line.qty}
                    onChange={(e) => updateLine(line.key, { qty: e.target.value })}
                    error={line.qty.trim() !== '' && !qtyValid(line.qty) ? 'Must be > 0' : undefined}
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
                  <TextField
                    label="Cost price ₹"
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
                  <TextField
                    label="Sell price ₹"
                    required
                    inputMode="decimal"
                    value={line.sell}
                    onChange={(e) => updateLine(line.key, { sell: e.target.value })}
                    error={
                      line.sell.trim() !== '' && rupeesToPaise(line.sell) == null
                        ? 'Invalid amount'
                        : undefined
                    }
                  />
                  <TextField
                    label="Freight ₹ (optional)"
                    inputMode="decimal"
                    value={line.freight}
                    onChange={(e) => updateLine(line.key, { freight: e.target.value })}
                    error={!optionalMoneyValid(line.freight) ? 'Invalid amount' : undefined}
                  />
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
                    label="Description (optional)"
                    value={line.description}
                    onChange={(e) => updateLine(line.key, { description: e.target.value })}
                    maxLength={500}
                  />
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Button onClick={onSubmit} disabled={!canSubmit} loading={create.isPending}>
            Create purchase order
          </Button>
          {!canSubmit && !create.isPending && (
            <span className="text-xs text-slate-500">
              Fill the PO number, client, project, date, and at least one valid line.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
