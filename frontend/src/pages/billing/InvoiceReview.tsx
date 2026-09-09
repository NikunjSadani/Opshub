import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
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
import { usePermissions } from '../../auth/AuthProvider';
import { usePurchaseOrderQuery, type POLine } from '../../api/purchaseOrders';
import { useProjectsQuery } from '../../api/projects';
import {
  useAddInvoiceLine,
  useBillingInvoiceQuery,
  useCancelInvoice,
  useDeleteInvoice,
  useDeleteInvoiceLine,
  useManualMatch,
  useRematch,
  useSetInvoiceProject,
  useSubmitReview,
  useUpdateInvoiceLine,
  type BillingCorrection,
  type BillingFieldOut,
  type BillingInvoiceDetail,
  type BillingLineOut,
  type LineWrite,
} from '../../api/billingInvoices';
import { BILLING_BASE } from './billingFormat';
import {
  FIELD_STATUS_LABEL,
  FIELD_STATUS_TONE,
  INVOICE_STATUS_LABEL,
  INVOICE_STATUS_TONE,
  MATCH_STATUS_LABEL,
  MATCH_STATUS_TONE,
  REQUIRED_FIELD_PATHS,
  errorMessage,
  fieldLabel,
  formatConfidence,
  isMoneyField,
  money,
  parseRupeesToPaise,
} from './billingInvoiceFormat';

/** Statuses from which corrections / matching / confirm are still permitted. */
const EDITABLE_STATUSES = new Set([
  'UPLOADED',
  'EXTRACTED',
  'NEEDS_REVIEW',
  'NEEDS_OCR',
  'NEEDS_MATCH',
  'MATCHED',
]);

/** A field is editable in review only while it is flagged low/missing. */
function isEditable(f: BillingFieldOut): boolean {
  return f.status === 'LOW_CONFIDENCE' || f.status === 'MISSING';
}

/** A required field is "resolved" when it reads clean (OK/CORRECTED) OR has a non-empty edit. */
function isResolved(f: BillingFieldOut | undefined, edit: string | undefined): boolean {
  if (edit !== undefined && edit.trim() !== '') return true;
  return f != null && (f.status === 'OK' || f.status === 'CORRECTED');
}

function sectionOf(fieldPath: string): 'header' | 'totals' | 'other' {
  if (fieldPath.startsWith('header.')) return 'header';
  if (fieldPath.startsWith('totals.')) return 'totals';
  return 'other';
}

const FIELD_CONTROL =
  'w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40';

function FieldRow({
  field,
  edit,
  error,
  onEdit,
}: {
  field: BillingFieldOut;
  edit: string | undefined;
  error?: boolean;
  onEdit: (value: string) => void;
}) {
  const inputId = `field-${field.field_path}`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const editable = isEditable(field);
  const isMoney = isMoneyField(field.field_path);
  // Money fields are EDITED in rupees (the read path stores integer paise), so an
  // untouched money value shows value_norm/100 with 2 decimals. Raw OCR text is not
  // prefilled for money — it is often the exact misread that flagged the field.
  const shown = isMoney
    ? edit ?? (field.value_norm != null ? (Number(field.value_norm) / 100).toFixed(2) : '')
    : edit ?? field.value_norm ?? field.value_raw ?? '';
  const hint = isMoney
    ? 'Enter amount in ₹'
    : field.value_raw
      ? `Extracted text: “${field.value_raw}”`
      : undefined;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ');
  return (
    <div className="grid grid-cols-1 gap-1 border-t border-slate-100 py-3 sm:grid-cols-[14rem_1fr] sm:gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <label
          htmlFor={editable ? inputId : undefined}
          className="text-sm font-medium text-slate-700"
        >
          {fieldLabel(field.field_path)}
        </label>
        <Badge tone={FIELD_STATUS_TONE[field.status]}>{FIELD_STATUS_LABEL[field.status]}</Badge>
        <span className="text-xs tabular-nums text-slate-400">
          {formatConfidence(field.confidence)}
        </span>
      </div>
      <div>
        {editable ? (
          <>
            <div className={isMoney ? 'relative' : undefined}>
              {isMoney && (
                <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-sm text-slate-500">
                  ₹
                </span>
              )}
              <input
                id={inputId}
                value={shown}
                onChange={(e) => onEdit(e.target.value)}
                inputMode={isMoney ? 'decimal' : undefined}
                aria-describedby={describedBy || undefined}
                aria-invalid={error || undefined}
                placeholder={
                  isMoney ? '0.00' : field.status === 'MISSING' ? 'Not found — enter a value' : undefined
                }
                className={`${FIELD_CONTROL}${isMoney ? ' pl-7' : ''}${
                  error ? ' border-red-400 focus:border-red-500 focus:ring-red-500/40' : ''
                }`}
              />
            </div>
            {error && (
              <span id={errorId} className="mt-1 block text-xs text-red-600">
                Enter a valid rupee amount (numbers, up to 2 decimals).
              </span>
            )}
            {hint && (
              <span id={hintId} className="mt-1 block text-xs text-slate-400">
                {hint}
              </span>
            )}
          </>
        ) : (
          <p className="text-sm text-slate-900">
            {isMoney && field.value_norm != null ? (
              money(Number(field.value_norm))
            ) : (
              field.value_norm || field.value_raw || <span className="text-slate-400">—</span>
            )}
          </p>
        )}
      </div>
    </div>
  );
}

/** A concise label for a PO line offered as a manual-match target. */
function poLineLabel(l: POLine): string {
  const name = l.product_name || l.description || `Line ${l.id}`;
  return `${name} · qty ${l.ordered_qty} · ${money(l.sell_price_paise)}`;
}

// --- manual line-item editor --------------------------------------------------

/** Form values for the add/edit line modal. Every input is a string; money is in ₹. */
interface LineFormValues {
  description: string;
  hsn_sac: string;
  quantity: string;
  unit: string;
  unit_rate: string;
  taxable: string;
  gst_rate: string;
  cgst: string;
  sgst: string;
  igst: string;
  line_total: string;
}

/** The integer-paise money keys on the wire (a narrow subset of LineWrite). */
type LineMoneyKey =
  | 'unit_rate_paise'
  | 'taxable_paise'
  | 'cgst_paise'
  | 'sgst_paise'
  | 'igst_paise'
  | 'line_total_paise';

/** The money form-fields, each mapped to its integer-paise LineWrite key + label. */
const LINE_MONEY_FIELDS: {
  key: keyof LineFormValues;
  paiseKey: LineMoneyKey;
  label: string;
}[] = [
  { key: 'unit_rate', paiseKey: 'unit_rate_paise', label: 'Unit rate (₹)' },
  { key: 'taxable', paiseKey: 'taxable_paise', label: 'Taxable (₹)' },
  { key: 'cgst', paiseKey: 'cgst_paise', label: 'CGST (₹)' },
  { key: 'sgst', paiseKey: 'sgst_paise', label: 'SGST (₹)' },
  { key: 'igst', paiseKey: 'igst_paise', label: 'IGST (₹)' },
  { key: 'line_total', paiseKey: 'line_total_paise', label: 'Line total (₹)' },
];

const EMPTY_LINE_FORM: LineFormValues = {
  description: '',
  hsn_sac: '',
  quantity: '',
  unit: '',
  unit_rate: '',
  taxable: '',
  gst_rate: '',
  cgst: '',
  sgst: '',
  igst: '',
  line_total: '',
};

/** Format an integer-paise money value into a rupee string for editing (blank when null). */
function paiseToRupeeInput(paise: number | null): string {
  return paise == null ? '' : (paise / 100).toFixed(2);
}

/** Pre-fill the form from an existing line (paise→₹ for money; strings verbatim). */
function lineToForm(line: BillingLineOut): LineFormValues {
  return {
    description: line.description ?? '',
    hsn_sac: line.hsn_sac ?? '',
    quantity: line.quantity ?? '',
    unit: line.unit ?? '',
    unit_rate: paiseToRupeeInput(line.unit_rate_paise),
    taxable: paiseToRupeeInput(line.taxable_paise),
    gst_rate: line.gst_rate ?? '',
    cgst: paiseToRupeeInput(line.cgst_paise),
    sgst: paiseToRupeeInput(line.sgst_paise),
    igst: paiseToRupeeInput(line.igst_paise),
    line_total: paiseToRupeeInput(line.line_total_paise),
  };
}

/**
 * Build the wire LineWrite from the form: description always sent (required, trimmed),
 * every other key sent ONLY when the operator left a non-empty value — money converted
 * ₹→paise via parseRupeesToPaise, quantity/gst_rate sent as trimmed strings. Assumes the
 * caller has already blocked submit on a blank description / an unparseable money field.
 */
function formToLineWrite(v: LineFormValues): LineWrite {
  const body: LineWrite = { description: v.description.trim() };
  if (v.hsn_sac.trim()) body.hsn_sac = v.hsn_sac.trim();
  if (v.quantity.trim()) body.quantity = v.quantity.trim();
  if (v.unit.trim()) body.unit = v.unit.trim();
  if (v.gst_rate.trim()) body.gst_rate = v.gst_rate.trim();
  for (const { key, paiseKey } of LINE_MONEY_FIELDS) {
    const raw = v[key].trim();
    if (raw === '') continue;
    const paise = parseRupeesToPaise(raw);
    if (paise != null) body[paiseKey] = paise;
  }
  return body;
}

// Client-side scale/range checks that MIRROR the backend guards, so a natural mistake gets an
// inline error instead of an opaque 422: quantity is Numeric(14,3), gst_rate Numeric(5,2) in
// 0..100. Both are non-negative (a line has no negative amount — credits are separate notes).
const QTY_RE = /^\d+(\.\d{1,3})?$/;
const GST_RE = /^\d+(\.\d{1,2})?$/;

function quantityError(raw: string): string | undefined {
  const v = raw.trim();
  if (v === '' || QTY_RE.test(v)) return undefined;
  return 'Enter a quantity — a number with up to 3 decimals.';
}
function gstRateError(raw: string): string | undefined {
  const v = raw.trim();
  if (v === '') return undefined;
  if (!GST_RE.test(v) || Number(v) > 100) return 'Enter a GST rate from 0 to 100 (up to 2 decimals).';
  return undefined;
}

/**
 * Modal form to add a new line item or edit an existing one. Money inputs are RUPEES,
 * validated with parseRupeesToPaise (an inline error on an unparseable amount); submit is
 * disabled while the description is empty or any money field is invalid.
 */
function LineFormModal({
  open,
  mode,
  initialLine,
  saving,
  onSubmit,
  onClose,
}: {
  open: boolean;
  mode: 'add' | 'edit';
  initialLine: BillingLineOut | null;
  saving: boolean;
  onSubmit: (body: LineWrite) => void;
  onClose: () => void;
}) {
  const [values, setValues] = useState<LineFormValues>(EMPTY_LINE_FORM);
  // The Description required-error shows only once the field has been touched (or a submit
  // attempted), so a freshly-opened Add form isn't greeted by a red error on a pristine field.
  const [descTouched, setDescTouched] = useState(false);
  // Re-seed the form each time the modal opens (add → blank, edit → the line's values).
  const seed = mode === 'edit' && initialLine ? lineToForm(initialLine) : EMPTY_LINE_FORM;
  const [seeded, setSeeded] = useState(false);
  if (open && !seeded) {
    setValues(seed);
    setSeeded(true);
    setDescTouched(false);
  }
  if (!open && seeded) setSeeded(false);

  function set(key: keyof LineFormValues, value: string) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  const moneyErrors = new Set(
    LINE_MONEY_FIELDS.filter(({ key }) => {
      const raw = values[key].trim();
      return raw !== '' && parseRupeesToPaise(raw) == null;
    }).map(({ key }) => key),
  );
  const qtyErr = quantityError(values.quantity);
  const gstErr = gstRateError(values.gst_rate);
  const descEmpty = values.description.trim() === '';
  const canSubmit = !descEmpty && moneyErrors.size === 0 && !qtyErr && !gstErr && !saving;

  function submit() {
    if (!canSubmit) return;
    onSubmit(formToLineWrite(values));
  }

  const moneyError = 'Enter a valid rupee amount (numbers, up to 2 decimals).';

  return (
    <Modal
      open={open}
      title={mode === 'add' ? 'Add line item' : `Edit line ${initialLine?.line_no ?? ''}`}
      onClose={onClose}
      busy={saving}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSubmit} loading={saving}>
            {mode === 'add' ? 'Add line' : 'Save line'}
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
        <TextField
          label="Description"
          required
          maxLength={500}
          value={values.description}
          onChange={(e) => set('description', e.target.value)}
          onBlur={() => setDescTouched(true)}
          error={descTouched && descEmpty ? 'Description is required.' : undefined}
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField
            label="HSN/SAC"
            maxLength={10}
            value={values.hsn_sac}
            onChange={(e) => set('hsn_sac', e.target.value)}
          />
          <TextField
            label="Quantity"
            inputMode="decimal"
            value={values.quantity}
            onChange={(e) => set('quantity', e.target.value)}
            error={qtyErr}
          />
          <TextField
            label="Unit"
            maxLength={20}
            value={values.unit}
            onChange={(e) => set('unit', e.target.value)}
          />
          <TextField
            label="GST rate (%)"
            inputMode="decimal"
            value={values.gst_rate}
            onChange={(e) => set('gst_rate', e.target.value)}
            error={gstErr}
          />
          {LINE_MONEY_FIELDS.map(({ key, label }) => (
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
        </div>
      </form>
    </Modal>
  );
}

function LinesMatchTable({
  invoice,
  poLines,
  poLoading,
  canOperate,
  isEditableStatus,
  onMap,
  onEdit,
  onDelete,
  mappingLineId,
}: {
  invoice: BillingInvoiceDetail;
  poLines: POLine[];
  poLoading: boolean;
  canOperate: boolean;
  isEditableStatus: boolean;
  onMap: (line: BillingLineOut, poLineItemId: string) => void;
  onEdit: (line: BillingLineOut) => void;
  onDelete: (line: BillingLineOut) => void;
  mappingLineId: string | null;
}) {
  if (invoice.lines.length === 0) {
    // Neutral copy: the invoice may be lineless because none were extracted OR because they
    // were deleted — don't claim "none were extracted". Point operators to the Add line button.
    return (
      <StatePanel title="No line items">
        {canOperate && isEditableStatus
          ? 'This invoice has no line items — use “Add line” above to add one.'
          : 'This invoice has no line items.'}
      </StatePanel>
    );
  }

  const poLineById = new Map(poLines.map((l) => [String(l.id), l]));
  const openLines = poLines.filter((l) => l.line_status === 'OPEN');
  const noPo = invoice.po_id == null;
  // The per-line Edit/Delete affordances only when the operator can act on an editable invoice.
  const canEditLines = canOperate && isEditableStatus;

  return (
    <Table>
      <THead>
        <Tr>
          <Th>#</Th>
          <Th>Description</Th>
          <Th className="text-right">Qty</Th>
          <Th className="text-right">Rate</Th>
          <Th className="text-right">Taxable</Th>
          <Th className="text-right">Line total</Th>
          {/* No PO to match against on a standalone invoice — drop the Match column. */}
          {!noPo && <Th>Match</Th>}
          {canEditLines && <Th className="text-right">Actions</Th>}
        </Tr>
      </THead>
      <tbody>
        {invoice.lines.map((l) => {
          const matched = l.po_line_item_id != null ? poLineById.get(l.po_line_item_id) : undefined;
          // Offer the OPEN PO lines, plus the currently-matched line even if it is no
          // longer OPEN — so the control's current value is always representable.
          const options = matched && matched.line_status !== 'OPEN' ? [matched, ...openLines] : openLines;
          const busy = mappingLineId === l.id;
          return (
            <Tr key={l.id}>
              <Td className="tabular-nums text-slate-500">{l.line_no}</Td>
              <Td className="text-slate-900">{l.description ?? '—'}</Td>
              <Td className="text-right tabular-nums">{l.quantity ?? '—'}</Td>
              <Td className="text-right tabular-nums">{money(l.unit_rate_paise)}</Td>
              <Td className="text-right tabular-nums">{money(l.taxable_paise)}</Td>
              <Td className="text-right tabular-nums">{money(l.line_total_paise)}</Td>
              {!noPo && (
                <Td>
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-2">
                      <Badge tone={MATCH_STATUS_TONE[l.match_status]}>
                        {MATCH_STATUS_LABEL[l.match_status]}
                      </Badge>
                      {busy && <span className="text-xs text-slate-400">Saving…</span>}
                    </div>
                    {matched && (
                      <span className="text-xs text-slate-500">→ {poLineLabel(matched)}</span>
                    )}
                    <select
                      aria-label={`Match line ${l.line_no} to a PO line`}
                      value={l.po_line_item_id ?? ''}
                      disabled={!canOperate || busy || poLoading}
                      onChange={(e) => {
                        if (e.target.value) onMap(l, e.target.value);
                      }}
                      className="w-full max-w-xs rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:bg-slate-50 disabled:text-slate-400"
                    >
                      <option value="">
                        {poLoading
                          ? 'Loading PO lines…'
                          : openLines.length === 0 && !matched
                            ? 'No open PO lines'
                            : 'Map to a PO line…'}
                      </option>
                      {options.map((po) => (
                        <option key={po.id} value={po.id}>
                          {poLineLabel(po)}
                          {po.line_status !== 'OPEN' ? ` (${po.line_status.toLowerCase()})` : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                </Td>
              )}
              {canEditLines && (
                <Td className="text-right">
                  <div className="flex items-center justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Edit line ${l.line_no}`}
                      onClick={() => onEdit(l)}
                    >
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Delete line ${l.line_no}`}
                      onClick={() => onDelete(l)}
                    >
                      Delete
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

export function InvoiceReview() {
  const toast = useToast();
  const navigate = useNavigate();
  const params = useParams();
  const invoiceId = params.id ?? null;
  const perms = usePermissions();
  const canOperate = perms.atLeast('billing', 'OPERATE');
  const canManage = perms.atLeast('billing', 'MANAGE');

  const query = useBillingInvoiceQuery(invoiceId);
  const invoice = query.data;

  const poQuery = usePurchaseOrderQuery(invoice?.po_id ?? null);
  const poLines = poQuery.data?.lines ?? [];

  const submit = useSubmitReview();
  const rematch = useRematch();
  const manualMatch = useManualMatch();
  const cancel = useCancelInvoice();
  const del = useDeleteInvoice();
  const addLine = useAddInvoiceLine();
  const updateLine = useUpdateInvoiceLine();
  const deleteLine = useDeleteInvoiceLine();
  // A PO-less invoice can be attributed directly to one of its client's ACTIVE projects.
  // Scoped to the invoice's client; only used/rendered on the standalone (no-PO) path.
  const projectsQuery = useProjectsQuery({
    client_id: invoice ? String(invoice.client_id) : '',
    status: 'ACTIVE',
  });
  const setProject = useSetInvoiceProject(invoice?.id ?? '');

  const [edits, setEdits] = useState<Record<string, string>>({});
  const [mappingLineId, setMappingLineId] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // The line editor: `null` closed, `{ mode:'add' }` a new line, `{ mode:'edit', line }` an
  // existing one. `deleteLineTarget` drives the per-line delete confirm dialog.
  const [lineForm, setLineForm] = useState<{ mode: 'add' | 'edit'; line: BillingLineOut | null } | null>(
    null,
  );
  const [deleteLineTarget, setDeleteLineTarget] = useState<BillingLineOut | null>(null);

  function setEdit(fieldPath: string, value: string) {
    setEdits((prev) => ({ ...prev, [fieldPath]: value }));
  }

  const backLink = (
    <Link to={BILLING_BASE} className="text-sm font-medium text-brand-600 hover:text-brand-700">
      ← Back to register
    </Link>
  );

  if (invoiceId == null || invoiceId === '') {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <StatePanel tone="red" title="Invalid invoice">
          That invoice link is not valid.
        </StatePanel>
      </div>
    );
  }

  if (query.isPending) return <Loading label="Loading invoice…" />;
  if (query.isError || !invoice) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const fieldByPath = new Map(invoice.fields.map((f) => [f.field_path, f]));
  const requiredResolved = REQUIRED_FIELD_PATHS.every((path) =>
    isResolved(fieldByPath.get(path), edits[path]),
  );
  const hasLines = invoice.lines.length > 0;
  // A PO-less invoice (po_id null) is a standalone AR record: there is no purchase order
  // to match lines against, so line-matching is neither shown nor required. The backend
  // confirms it once the required fields are in order (derived status MATCHED/ready).
  const noPo = invoice.po_id == null;
  // Direct project attribution is a PO-less concern (a PO carries its own project). Resolve
  // the assigned project's display name from the client's ACTIVE projects (falling back to
  // its id if it is no longer active / not in the list).
  const activeProjects = projectsQuery.data ?? [];
  const assignedProject =
    invoice.project_id != null
      ? activeProjects.find((p) => String(p.id) === String(invoice.project_id))
      : undefined;
  const allLinesMatched =
    hasLines && invoice.lines.every((l) => l.match_status === 'MATCHED' || l.match_status === 'MANUAL');
  const isEditableStatus = EDITABLE_STATUSES.has(invoice.status);
  const alreadyConfirmed = invoice.status === 'CONFIRMED';
  const isCancelled = invoice.status === 'CANCELLED';
  const isRejected = invoice.status === 'REJECTED';

  const moneyErrorPaths = new Set(
    Object.entries(edits)
      .filter(([path, v]) => isMoneyField(path) && v.trim() !== '' && parseRupeesToPaise(v) == null)
      .map(([path]) => path),
  );
  const hasMoneyError = moneyErrorPaths.size > 0;

  const canConfirm =
    canOperate &&
    isEditableStatus &&
    hasLines &&
    requiredResolved &&
    (noPo || allLinesMatched) &&
    !hasMoneyError;

  const headerFields = invoice.fields.filter((f) => sectionOf(f.field_path) === 'header');
  const totalsFields = invoice.fields.filter((f) => sectionOf(f.field_path) === 'totals');
  const otherFields = invoice.fields.filter((f) => sectionOf(f.field_path) === 'other');

  function buildCorrections(): BillingCorrection[] {
    return Object.entries(edits).flatMap(([field_path, raw]) => {
      const v = raw.trim();
      if (v === '') return [];
      if (isMoneyField(field_path)) {
        const paise = parseRupeesToPaise(v);
        return paise == null ? [] : [{ field_path, value: String(paise) }];
      }
      return [{ field_path, value: v }];
    });
  }

  const hasEdits = Object.values(edits).some((v) => v.trim() !== '');

  function onConfirm() {
    if (!canConfirm) return;
    submit.mutate(
      { invoiceId: invoice!.id, corrections: buildCorrections(), confirm: true },
      {
        onSuccess: (updated) => {
          setEdits({});
          if (updated.status === 'CONFIRMED') {
            toast.success('Invoice confirmed.');
          } else {
            toast.info(`Saved — status is now ${INVOICE_STATUS_LABEL[updated.status]}.`);
          }
        },
        // Surface the backend's honest 409 reason (weak field / unmatched line / clash).
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function onSaveDraft() {
    if (hasMoneyError) return;
    const corrections = buildCorrections();
    if (corrections.length === 0) return;
    submit.mutate(
      { invoiceId: invoice!.id, corrections, confirm: false },
      {
        onSuccess: () => {
          setEdits({});
          toast.success('Corrections saved.');
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function onRematch() {
    rematch.mutate(invoice!.id, {
      onSuccess: (updated) => {
        const matched = updated.lines.filter(
          (l) => l.match_status === 'MATCHED' || l.match_status === 'MANUAL',
        ).length;
        toast.info(`Re-matched — ${matched}/${updated.lines.length} line(s) matched.`);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  function onMap(line: BillingLineOut, poLineItemId: string) {
    setMappingLineId(line.id);
    manualMatch.mutate(
      { invoiceId: invoice!.id, lineId: line.id, poLineItemId },
      {
        onSuccess: () => toast.success(`Line ${line.line_no} mapped.`),
        onError: (err) => toast.error(errorMessage(err)),
        onSettled: () => setMappingLineId(null),
      },
    );
  }

  function onSetProject(value: string) {
    const projectId = value ? Number(value) : null;
    setProject.mutate(projectId, {
      onSuccess: () =>
        toast.success(projectId == null ? 'Project attribution cleared.' : 'Project updated.'),
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  function onCancelInvoice() {
    cancel.mutate(invoice!.id, {
      onSuccess: () => {
        setConfirmCancel(false);
        toast.success('Invoice cancelled.');
      },
      onError: (err) => {
        setConfirmCancel(false);
        toast.error(errorMessage(err));
      },
    });
  }

  function onDeleteInvoice() {
    del.mutate(invoice!.id, {
      onSuccess: () => {
        setConfirmDelete(false);
        toast.success('Invoice deleted.');
        navigate(BILLING_BASE);
      },
      onError: (err) => {
        setConfirmDelete(false);
        toast.error(errorMessage(err));
      },
    });
  }

  function onSubmitLine(body: LineWrite) {
    const editing = lineForm?.mode === 'edit' ? lineForm.line : null;
    if (editing) {
      updateLine.mutate(
        { invoiceId: invoice!.id, lineId: editing.id, body },
        {
          onSuccess: () => {
            setLineForm(null);
            toast.success('Line updated.');
          },
          onError: (err) => toast.error(errorMessage(err)),
        },
      );
    } else {
      addLine.mutate(
        { invoiceId: invoice!.id, body },
        {
          onSuccess: () => {
            setLineForm(null);
            toast.success('Line added.');
          },
          onError: (err) => toast.error(errorMessage(err)),
        },
      );
    }
  }

  function onDeleteLine() {
    const line = deleteLineTarget;
    if (!line) return;
    deleteLine.mutate(
      { invoiceId: invoice!.id, lineId: line.id },
      {
        onSuccess: () => {
          setDeleteLineTarget(null);
          toast.success('Line deleted.');
          // The deleted row held the focused Delete button and now unmounts, so move focus to
          // a stable anchor (the Add line button) instead of letting it fall to <body>.
          requestAnimationFrame(() => document.getElementById('invoice-add-line')?.focus());
        },
        onError: (err) => {
          setDeleteLineTarget(null);
          toast.error(errorMessage(err));
        },
      },
    );
  }

  const confirmReason = hasMoneyError
    ? 'Fix the highlighted amount to confirm.'
    : !hasLines
      ? 'At least one line item is required to confirm.'
      : !requiredResolved
        ? 'Resolve every required field (buyer GSTIN, invoice number, date, taxable, grand total) to confirm.'
        : !noPo && !allLinesMatched
          ? 'Match every line to a PO line to confirm.'
          : !canOperate
            ? 'You need Operate access to confirm this invoice.'
            : '';

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={invoice.invoice_number ? `Invoice ${invoice.invoice_number}` : `Invoice #${invoice.id}`}
        subtitle={invoice.buyer_gstin ? `Buyer GSTIN ${invoice.buyer_gstin}` : undefined}
        actions={
          <Badge tone={INVOICE_STATUS_TONE[invoice.status]}>
            {INVOICE_STATUS_LABEL[invoice.status]}
          </Badge>
        }
      />

      {invoice.review_reasons.length > 0 && (
        <div
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          <p className="mb-1 font-semibold">Why this needs a look</p>
          <ul className="list-disc pl-5">
            {invoice.review_reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {invoice.needs_ocr && (
        <div
          role="status"
          className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600"
        >
          This PDF has no text layer (scanned/image). OCR is not available yet, so its fields could
          not be extracted automatically.
        </div>
      )}

      <Card className="mb-6 p-5">
        <h2 className="mb-1 text-sm font-semibold text-slate-900">Header</h2>
        <p className="mb-2 text-xs text-slate-500">
          Fields flagged low-confidence or missing are editable. Others show the extracted value.
        </p>
        {headerFields.length === 0 ? (
          <p className="text-sm text-slate-400">No header fields extracted.</p>
        ) : (
          headerFields.map((f) => (
            <FieldRow
              key={f.field_path}
              field={f}
              edit={edits[f.field_path]}
              error={moneyErrorPaths.has(f.field_path)}
              onEdit={(v) => setEdit(f.field_path, v)}
            />
          ))
        )}
      </Card>

      <Card className="mb-6 p-5">
        <h2 className="mb-2 text-sm font-semibold text-slate-900">Totals</h2>
        {totalsFields.length === 0 ? (
          <p className="text-sm text-slate-400">No totals extracted.</p>
        ) : (
          totalsFields.map((f) => (
            <FieldRow
              key={f.field_path}
              field={f}
              edit={edits[f.field_path]}
              error={moneyErrorPaths.has(f.field_path)}
              onEdit={(v) => setEdit(f.field_path, v)}
            />
          ))
        )}
      </Card>

      {otherFields.length > 0 && (
        <Card className="mb-6 p-5">
          <h2 className="mb-2 text-sm font-semibold text-slate-900">Other fields</h2>
          {otherFields.map((f) => (
            <FieldRow
              key={f.field_path}
              field={f}
              edit={edits[f.field_path]}
              error={moneyErrorPaths.has(f.field_path)}
              onEdit={(v) => setEdit(f.field_path, v)}
            />
          ))}
        </Card>
      )}

      <div className="mb-6">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">
            {noPo ? 'Line items' : 'Line items & PO matching'}
          </h2>
          <div className="flex items-center gap-2">
            {/* Add lives in the header (OUTSIDE the table) so a lineless invoice — whose
                table early-returns a "No line items" panel — can still get its first line. */}
            {canOperate && isEditableStatus && (
              <Button
                id="invoice-add-line"
                variant="secondary"
                size="sm"
                onClick={() => setLineForm({ mode: 'add', line: null })}
                disabled={addLine.isPending}
              >
                Add line
              </Button>
            )}
            {!noPo && isEditableStatus && (
              <Button
                variant="secondary"
                size="sm"
                onClick={onRematch}
                disabled={!canOperate || rematch.isPending}
                loading={rematch.isPending}
              >
                Re-match
              </Button>
            )}
          </div>
        </div>
        {canOperate && isEditableStatus && (
          <p className="mb-2 text-xs text-slate-400">
            {noPo
              ? "Editing lines doesn't change the invoice's grand total (the receivable) — the lines are a record of what's billed."
              : "Editing lines doesn't change the invoice's grand total (the receivable) — it drives PO matching and the invoiced-quantity rollup."}
          </p>
        )}
        {noPo ? (
          <div className="mb-2 space-y-2">
            <div
              role="status"
              className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600"
            >
              No PO linked — lines aren't matched to a purchase order. This invoice will be
              confirmed as a standalone AR record.
            </div>
            <div className="rounded-md border border-slate-200 bg-white px-3 py-2">
              <label
                htmlFor="invoice-project"
                className="mb-1 block text-xs font-medium text-slate-600"
              >
                Project attribution
              </label>
              {alreadyConfirmed || !canOperate ? (
                <p className="text-sm text-slate-900">
                  {assignedProject ? (
                    `${assignedProject.code} — ${assignedProject.name}`
                  ) : invoice.project_id != null ? (
                    `Project #${invoice.project_id}`
                  ) : (
                    <span className="text-slate-400">No project</span>
                  )}
                </p>
              ) : (
                <select
                  id="invoice-project"
                  aria-label="Project attribution"
                  value={invoice.project_id != null ? String(invoice.project_id) : ''}
                  disabled={setProject.isPending || projectsQuery.isPending}
                  onChange={(e) => onSetProject(e.target.value)}
                  className="w-full max-w-sm rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:bg-slate-50 disabled:text-slate-400"
                >
                  <option value="">
                    {projectsQuery.isPending ? 'Loading projects…' : 'No project'}
                  </option>
                  {activeProjects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.code} — {p.name}
                    </option>
                  ))}
                </select>
              )}
              <p className="mt-1 text-xs text-slate-400">
                Attribute this invoice's revenue to a project (optional — used for P&L).
              </p>
            </div>
          </div>
        ) : (
          poQuery.isError && (
            <div className="mb-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Could not load the purchase order's lines — manual matching is unavailable until it loads.
            </div>
          )
        )}
        <LinesMatchTable
          invoice={invoice}
          poLines={poLines}
          poLoading={invoice.po_id != null && poQuery.isPending}
          canOperate={canOperate}
          isEditableStatus={isEditableStatus}
          onMap={onMap}
          onEdit={(line) => setLineForm({ mode: 'edit', line })}
          onDelete={(line) => setDeleteLineTarget(line)}
          mappingLineId={mappingLineId}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-slate-200 pt-4">
        {alreadyConfirmed ? (
          <p className="text-sm text-emerald-700">This invoice is confirmed. Its values are frozen.</p>
        ) : isCancelled ? (
          <p className="text-sm text-slate-500">This invoice was cancelled.</p>
        ) : isRejected ? (
          <p className="text-sm text-slate-500">
            This invoice was rejected at the quality gate and cannot be confirmed.
          </p>
        ) : !canOperate ? (
          <p className="text-sm text-slate-500">
            You have view-only access to this invoice — confirming and matching need Operate access.
          </p>
        ) : (
          <>
            <Button onClick={onConfirm} disabled={!canConfirm} loading={submit.isPending}>
              Confirm invoice
            </Button>
            {hasEdits && canOperate && (
              <Button
                variant="secondary"
                onClick={onSaveDraft}
                disabled={submit.isPending || hasMoneyError}
                loading={submit.isPending}
              >
                Save corrections
              </Button>
            )}
            {!canConfirm && confirmReason && (
              <span className="text-xs text-slate-500">{confirmReason}</span>
            )}
          </>
        )}

        {/* Cancel / Delete are MANAGE-only. Delete is always available (delete-and-re-upload);
            cancel only applies to a not-yet-terminal invoice. */}
        {canManage && (
          <div className="ml-auto flex items-center gap-2">
            {!alreadyConfirmed && !isCancelled && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmCancel(true)}
                disabled={cancel.isPending}
              >
                Cancel invoice
              </Button>
            )}
            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmDelete(true)}
              disabled={del.isPending}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmCancel}
        title="Cancel this invoice?"
        confirmLabel="Cancel invoice"
        danger
        loading={cancel.isPending}
        onCancel={() => setConfirmCancel(false)}
        onConfirm={onCancelInvoice}
        message="This soft-cancels the invoice. It stays in the register as Cancelled and cannot be confirmed unless reopened."
      />

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this invoice?"
        confirmLabel="Delete invoice"
        danger
        loading={del.isPending}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={onDeleteInvoice}
        message="This permanently deletes the invoice and its source PDF. This cannot be undone."
      />

      <LineFormModal
        open={lineForm != null}
        mode={lineForm?.mode ?? 'add'}
        initialLine={lineForm?.line ?? null}
        saving={addLine.isPending || updateLine.isPending}
        onSubmit={onSubmitLine}
        onClose={() => setLineForm(null)}
      />

      <ConfirmDialog
        open={deleteLineTarget != null}
        title="Delete this line?"
        confirmLabel="Delete line"
        danger
        loading={deleteLine.isPending}
        onCancel={() => setDeleteLineTarget(null)}
        onConfirm={onDeleteLine}
        message={
          deleteLineTarget
            ? `Delete line ${deleteLineTarget.line_no}? This can't be undone.`
            : ''
        }
      />
    </div>
  );
}
