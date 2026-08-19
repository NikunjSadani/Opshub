import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import {
  useInvoice,
  useSubmitReview,
  type Correction,
  type FieldOut,
  type InvoiceDetail,
} from '../../api/expense';
import {
  EXPENSE_BASE,
  FIELD_STATUS_LABEL,
  FIELD_STATUS_TONE,
  INVOICE_STATUS_LABEL,
  INVOICE_STATUS_TONE,
  errorMessage,
  fieldLabel,
  formatConfidence,
  formatPaise,
  isMoneyField,
  parseRupeesToPaise,
} from './expenseFormat';

/**
 * The canonical fields that must be resolved (present + trustworthy) before an
 * invoice can be CONFIRMED. Mirrors the backend's REQUIRED set (DESIGN §Extraction
 * step 6). Line presence is enforced separately (≥1 line item).
 */
const REQUIRED_PATHS: readonly string[] = [
  'header.supplier_gstin',
  'header.invoice_number',
  'header.invoice_date',
  'totals.total_taxable_paise',
  'totals.grand_total_paise',
];

/** A field is editable in review only while it is flagged low/missing. */
function isEditable(f: FieldOut): boolean {
  return f.status === 'LOW_CONFIDENCE' || f.status === 'MISSING';
}

/**
 * A required field is "resolved" when it already reads clean (OK/CORRECTED) OR the
 * operator has typed a non-empty correction for it this session.
 */
function isResolved(f: FieldOut | undefined, edit: string | undefined): boolean {
  if (edit !== undefined && edit.trim() !== '') return true;
  return f != null && (f.status === 'OK' || f.status === 'CORRECTED');
}

/** Split fields into header vs totals vs line sections, preserving order. */
function sectionOf(fieldPath: string): 'header' | 'totals' | 'line' | 'other' {
  if (fieldPath.startsWith('header.')) return 'header';
  if (fieldPath.startsWith('totals.')) return 'totals';
  if (fieldPath.startsWith('line.')) return 'line';
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
  field: FieldOut;
  edit: string | undefined;
  /** True when this is a money field whose typed rupee value is not parseable. */
  error?: boolean;
  onEdit: (value: string) => void;
}) {
  const inputId = `field-${field.field_path}`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const editable = isEditable(field);
  const money = isMoneyField(field.field_path);
  // Money fields are EDITED in rupees (the read path stores integer paise), so an
  // untouched money value is shown as value_normalized/100 with 2 decimals. The
  // raw OCR text is deliberately not prefilled for money — it is often the exact
  // misread that flagged the field (e.g. "1I800.00").
  const shown = money
    ? (edit ??
        (field.value_normalized != null
          ? (Number(field.value_normalized) / 100).toFixed(2)
          : ''))
    : (edit ?? field.value_normalized ?? field.value_raw ?? '');
  const hint = money
    ? 'Enter amount in ₹'
    : field.value_raw
      ? `Extracted text: “${field.value_raw}”`
      : undefined;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ');
  return (
    <div className="grid grid-cols-1 gap-1 border-t border-slate-100 py-3 sm:grid-cols-[14rem_1fr] sm:gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={editable ? inputId : undefined} className="text-sm font-medium text-slate-700">
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
            <div className={money ? 'relative' : undefined}>
              {money && (
                <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-sm text-slate-500">
                  ₹
                </span>
              )}
              <input
                id={inputId}
                value={shown}
                onChange={(e) => onEdit(e.target.value)}
                inputMode={money ? 'decimal' : undefined}
                aria-describedby={describedBy || undefined}
                aria-invalid={error || undefined}
                placeholder={
                  money
                    ? '0.00'
                    : field.status === 'MISSING'
                      ? 'Not found — enter a value'
                      : undefined
                }
                className={`${FIELD_CONTROL}${money ? ' pl-7' : ''}${
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
            {money && field.value_normalized != null
              ? formatPaise(Number(field.value_normalized))
              : field.value_normalized || field.value_raw || <span className="text-slate-400">—</span>}
          </p>
        )}
      </div>
    </div>
  );
}

function LinesTable({ invoice }: { invoice: InvoiceDetail }) {
  if (invoice.lines.length === 0) {
    return (
      <StatePanel title="No line items">
        No line items were extracted for this invoice.
      </StatePanel>
    );
  }
  return (
    <Table>
      <THead>
        <Tr>
          <Th>#</Th>
          <Th>Description</Th>
          <Th>HSN/SAC</Th>
          <Th className="text-right">Qty</Th>
          <Th className="text-right">Rate</Th>
          <Th className="text-right">Taxable</Th>
          <Th className="text-right">GST %</Th>
          <Th className="text-right">Line total</Th>
        </Tr>
      </THead>
      <tbody>
        {invoice.lines.map((l) => (
          <Tr key={l.line_no}>
            <Td className="tabular-nums text-slate-500">{l.line_no}</Td>
            <Td className="text-slate-900">{l.description ?? '—'}</Td>
            <Td className="tabular-nums">{l.hsn_sac ?? '—'}</Td>
            <Td className="text-right tabular-nums">{l.quantity ?? '—'}</Td>
            <Td className="text-right tabular-nums">{formatPaise(l.unit_rate_paise)}</Td>
            <Td className="text-right tabular-nums">{formatPaise(l.taxable_paise)}</Td>
            <Td className="text-right tabular-nums">{l.gst_rate != null ? `${l.gst_rate}%` : '—'}</Td>
            <Td className="text-right tabular-nums">{formatPaise(l.line_total_paise)}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

export function ReviewPanel() {
  const toast = useToast();
  const params = useParams();
  const invoiceId = params.id != null ? Number(params.id) : null;
  const validId = invoiceId != null && Number.isFinite(invoiceId);

  const query = useInvoice(validId ? invoiceId : null);
  const submit = useSubmitReview();
  const invoice = query.data;

  // Pending corrections keyed by field_path (absent = untouched).
  const [edits, setEdits] = useState<Record<string, string>>({});

  function setEdit(fieldPath: string, value: string) {
    setEdits((prev) => ({ ...prev, [fieldPath]: value }));
  }

  const backLink = (
    <Link
      to={`${EXPENSE_BASE}/register`}
      className="text-sm font-medium text-brand-600 hover:text-brand-700"
    >
      ← Back to register
    </Link>
  );

  if (!validId) {
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
  const requiredResolved = REQUIRED_PATHS.every((path) =>
    isResolved(fieldByPath.get(path), edits[path]),
  );
  const hasLines = invoice.lines.length > 0;
  const alreadyConfirmed = invoice.status === 'CONFIRMED';
  const isParked = invoice.status === 'NEEDS_OCR' || invoice.status === 'REJECTED';
  // A money edit that is non-empty but doesn't parse to paise (letters, >2 decimals,
  // stray symbols) is a hard error — the backend would reject it. Flag such paths so
  // the row shows an inline error and both actions are blocked until it is fixed.
  const moneyErrorPaths = new Set(
    Object.entries(edits)
      .filter(([path, v]) => isMoneyField(path) && v.trim() !== '' && parseRupeesToPaise(v) == null)
      .map(([path]) => path),
  );
  const hasMoneyError = moneyErrorPaths.size > 0;
  // Confirm is available only when every required field is resolved, at least one
  // line exists, the invoice isn't already terminal (confirmed/parked), and no
  // money edit is malformed.
  const canConfirm =
    requiredResolved && hasLines && !alreadyConfirmed && !isParked && !hasMoneyError;

  const headerFields = invoice.fields.filter((f) => sectionOf(f.field_path) === 'header');
  const totalsFields = invoice.fields.filter((f) => sectionOf(f.field_path) === 'totals');
  const otherFields = invoice.fields.filter(
    (f) => sectionOf(f.field_path) === 'line' || sectionOf(f.field_path) === 'other',
  );

  // Build the wire corrections from the pending edits. Money fields are edited in
  // rupees but the backend coerces money with int(text), so each money edit is
  // converted to an integer-PAISE string here (the single ₹→paise seam for writes);
  // a non-parseable money edit is dropped (the buttons are already gated on it).
  function buildCorrections(): Correction[] {
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

  function onConfirm() {
    if (!canConfirm) return;
    const corrections = buildCorrections();
    submit.mutate(
      { invoiceId: invoice!.id, corrections, confirm: true },
      {
        onSuccess: (updated) => {
          setEdits({});
          if (updated.status === 'CONFIRMED') {
            toast.success('Invoice confirmed.');
          } else {
            toast.info(`Saved — status is now ${INVOICE_STATUS_LABEL[updated.status]}.`);
          }
        },
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

  const hasEdits = Object.values(edits).some((v) => v.trim() !== '');

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={invoice.invoice_number ? `Invoice ${invoice.invoice_number}` : `Invoice #${invoice.id}`}
        subtitle={invoice.supplier_name ?? undefined}
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
          not be extracted.
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
        <h2 className="mb-2 text-sm font-semibold text-slate-900">Line items</h2>
        <LinesTable invoice={invoice} />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-slate-200 pt-4">
        {alreadyConfirmed ? (
          <p className="text-sm text-emerald-700">This invoice is confirmed. Its values are frozen.</p>
        ) : isParked ? (
          <p className="text-sm text-slate-500">
            {invoice.status === 'NEEDS_OCR'
              ? 'This invoice is parked pending OCR and cannot be confirmed yet.'
              : 'This invoice was rejected at the quality gate and cannot be confirmed.'}
          </p>
        ) : (
          <>
            <Button onClick={onConfirm} disabled={!canConfirm} loading={submit.isPending}>
              Confirm invoice
            </Button>
            {hasEdits && (
              <Button
                variant="secondary"
                onClick={onSaveDraft}
                disabled={submit.isPending || hasMoneyError}
                loading={submit.isPending}
              >
                Save corrections
              </Button>
            )}
            {!canConfirm && (
              <span className="text-xs text-slate-500">
                {hasMoneyError
                  ? 'Fix the highlighted amount to confirm.'
                  : !hasLines
                    ? 'At least one line item is required to confirm.'
                    : 'Resolve every required field (supplier GSTIN, invoice number, date, taxable, grand total) to confirm.'}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  );
}
