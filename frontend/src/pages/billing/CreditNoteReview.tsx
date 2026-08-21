import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
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
import { usePermissions } from '../../auth/AuthProvider';
import { usePurchaseOrderQuery, type POLine } from '../../api/purchaseOrders';
import {
  useCancelCn,
  useCreditNoteQuery,
  useDeleteCn,
  useManualCnMatch,
  useRematchCn,
  useSubmitCnReview,
  type CnCorrection,
  type CnFieldOut,
  type CreditNoteDetail,
  type CreditNoteLine,
} from '../../api/billingCreditNotes';
import { BILLING_BASE } from './billingFormat';
import {
  FIELD_STATUS_LABEL,
  FIELD_STATUS_TONE,
  MATCH_STATUS_LABEL,
  MATCH_STATUS_TONE,
  errorMessage,
  formatConfidence,
  formatDate,
  isMoneyField,
  money,
  parseRupeesToPaise,
} from './billingInvoiceFormat';
import {
  CN_STATUS_LABEL,
  CN_STATUS_TONE,
  cnFieldLabel,
  referencedInvoiceTone,
} from './creditNoteFormat';

/** Statuses from which matching / confirm are still permitted. */
const EDITABLE_STATUSES = new Set([
  'UPLOADED',
  'EXTRACTED',
  'NEEDS_REVIEW',
  'NEEDS_OCR',
  'NEEDS_MATCH',
  'MATCHED',
]);

/** A field is editable in review only while it is flagged low-confidence or missing. */
function isEditableField(f: CnFieldOut): boolean {
  return f.status === 'LOW_CONFIDENCE' || f.status === 'MISSING';
}

/** Parse a Decimal qty string to a number; treat a missing value as 0 for the tally. */
function qtyNum(s: string | null): number {
  return s == null ? 0 : Number(s);
}

/** Show a qty as-is when whole, else to 2 dp — keeps the over-credit copy tidy. */
function fmtQty(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

const FIELD_CONTROL =
  'w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40';

/**
 * One flagged CN field: an editable input while LOW_CONFIDENCE/MISSING, else a read-only
 * value (a CORRECTED field reads as resolved). Mirrors the invoice lane's FieldRow —
 * money is edited in ₹ (the wire stores integer paise), status is conveyed by a text
 * badge (not colour alone), and hints/errors are wired via aria-describedby.
 */
function CnFieldRow({
  field,
  edit,
  error,
  onEdit,
}: {
  field: CnFieldOut;
  edit: string | undefined;
  error?: boolean;
  onEdit: (value: string) => void;
}) {
  const inputId = `cn-field-${field.field_path}`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const editable = isEditableField(field);
  const isMoney = isMoneyField(field.field_path);
  // Money fields are EDITED in rupees; an untouched money value shows value_norm/100.
  // Raw OCR text is not prefilled for money — it is often the exact misread that flagged it.
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
          {cnFieldLabel(field.field_path)}
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
                  isMoney
                    ? '0.00'
                    : field.status === 'MISSING'
                      ? 'Not found — enter a value'
                      : undefined
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

/** One row of the totals summary (read-only). */
function TotalRow({ label, value, signed }: { label: string; value: number | null; signed?: boolean }) {
  return (
    <div className="flex items-center justify-between border-t border-slate-100 py-2 text-sm">
      <span className="text-slate-600">{label}</span>
      <span className="tabular-nums text-slate-900">
        {value == null ? '—' : signed && value < 0 ? `-${money(-value)}` : money(value)}
      </span>
    </div>
  );
}

function LinesMatchTable({
  cn,
  poLines,
  poLoading,
  canOperate,
  onMap,
  mappingLineId,
}: {
  cn: CreditNoteDetail;
  poLines: POLine[];
  poLoading: boolean;
  canOperate: boolean;
  onMap: (line: CreditNoteLine, poLineItemId: string) => void;
  mappingLineId: string | null;
}) {
  if (cn.lines.length === 0) {
    return (
      <StatePanel title="No line items">No line items were extracted for this credit note.</StatePanel>
    );
  }

  const poLineById = new Map(poLines.map((l) => [String(l.id), l]));
  const openLines = poLines.filter((l) => l.line_status === 'OPEN');
  const noPo = cn.referenced_invoice?.po_id == null;

  return (
    <Table>
      <THead>
        <Tr>
          <Th>#</Th>
          <Th>Description</Th>
          <Th className="text-right">Qty</Th>
          <Th className="text-right">Taxable</Th>
          <Th className="text-right">Line total</Th>
          <Th>Match</Th>
        </Tr>
      </THead>
      <tbody>
        {cn.lines.map((l) => {
          const currentId = l.po_line_item_id != null ? String(l.po_line_item_id) : '';
          const matched = currentId ? poLineById.get(currentId) : undefined;
          // Offer the OPEN PO lines, plus the currently-matched line even if it is no
          // longer OPEN — so the control's current value is always representable.
          const options = matched && matched.line_status !== 'OPEN' ? [matched, ...openLines] : openLines;
          const busy = mappingLineId === l.id;
          // Over-credit tally (matched lines only — billed/already-credited are null when
          // UNMATCHED). credited-so-far = other confirmed CNs + THIS line's credit qty.
          const hasBilled = l.billed_qty != null;
          const creditedSoFar = qtyNum(l.already_credited_qty) + qtyNum(l.quantity);
          const overCredited = hasBilled && creditedSoFar > qtyNum(l.billed_qty);
          return (
            <Tr key={l.id}>
              <Td className="tabular-nums text-slate-500">{l.line_no}</Td>
              <Td className="text-slate-900">{l.description ?? '—'}</Td>
              <Td className="text-right tabular-nums">{l.quantity ?? '—'}</Td>
              <Td className="text-right tabular-nums">{money(l.taxable_paise)}</Td>
              <Td className="text-right tabular-nums">{money(l.line_total_paise)}</Td>
              <Td>
                <div className="flex flex-col gap-1.5">
                  <div className="flex items-center gap-2">
                    <Badge tone={MATCH_STATUS_TONE[l.match_status]}>
                      {MATCH_STATUS_LABEL[l.match_status]}
                    </Badge>
                    {busy && <span className="text-xs text-slate-400">Saving…</span>}
                  </div>
                  {l.po_line_label && (
                    <span className="text-xs text-slate-500">→ {l.po_line_label}</span>
                  )}
                  {hasBilled && (
                    <span className="text-xs text-slate-500">
                      Invoice billed {fmtQty(qtyNum(l.billed_qty))} · already credited{' '}
                      {fmtQty(qtyNum(l.already_credited_qty))}
                    </span>
                  )}
                  {overCredited && (
                    <span role="status" className="text-xs font-medium text-amber-700">
                      Credits {fmtQty(creditedSoFar)} but the invoice billed only{' '}
                      {fmtQty(qtyNum(l.billed_qty))} on this line.
                    </span>
                  )}
                  {noPo ? (
                    <span className="text-xs text-amber-700">
                      The credited invoice has no PO linked — lines cannot be matched.
                    </span>
                  ) : (
                    <select
                      aria-label={`Match line ${l.line_no} to a PO line`}
                      value={currentId}
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
                  )}
                </div>
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function CreditNoteReview() {
  const toast = useToast();
  const navigate = useNavigate();
  const params = useParams();
  const cnId = params.id ?? null;
  const perms = usePermissions();
  const canOperate = perms.atLeast('billing', 'OPERATE');
  const canManage = perms.atLeast('billing', 'MANAGE');

  const query = useCreditNoteQuery(cnId);
  const cn = query.data;

  const poId = cn?.referenced_invoice?.po_id ?? null;
  const poQuery = usePurchaseOrderQuery(poId);
  const poLines = poQuery.data?.lines ?? [];

  const submit = useSubmitCnReview();
  const rematch = useRematchCn();
  const manualMatch = useManualCnMatch();
  const cancel = useCancelCn();
  const del = useDeleteCn();

  const [edits, setEdits] = useState<Record<string, string>>({});
  const [mappingLineId, setMappingLineId] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  function setEdit(fieldPath: string, value: string) {
    setEdits((prev) => ({ ...prev, [fieldPath]: value }));
  }

  const backLink = (
    <Link
      to={`${BILLING_BASE}/credit-notes`}
      className="text-sm font-medium text-brand-600 hover:text-brand-700"
    >
      ← Back to credit notes
    </Link>
  );

  if (cnId == null || cnId === '') {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <StatePanel tone="red" title="Invalid credit note">
          That credit-note link is not valid.
        </StatePanel>
      </div>
    );
  }

  if (query.isPending) return <Loading label="Loading credit note…" />;
  if (query.isError || !cn) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const hasLines = cn.lines.length > 0;
  const allLinesMatched =
    hasLines && cn.lines.every((l) => l.match_status === 'MATCHED' || l.match_status === 'MANUAL');
  const isEditableStatus = EDITABLE_STATUSES.has(cn.status);
  const alreadyConfirmed = cn.status === 'CONFIRMED';
  const isCancelled = cn.status === 'CANCELLED';
  const isRejected = cn.status === 'REJECTED';
  const refInvoiceConfirmed = cn.referenced_invoice?.status === 'CONFIRMED';

  // Flagged header/total fields (LOW_CONFIDENCE / MISSING / CORRECTED) surface in the
  // editor card; a CORRECTED field reads as resolved. Clean (OK) fields stay in the
  // read-only totals summary.
  const flaggedFields = cn.fields.filter((f) => f.status !== 'OK');

  const moneyErrorPaths = new Set(
    Object.entries(edits)
      .filter(([path, v]) => isMoneyField(path) && v.trim() !== '' && parseRupeesToPaise(v) == null)
      .map(([path]) => path),
  );
  const hasMoneyError = moneyErrorPaths.size > 0;
  const hasEdits = Object.values(edits).some((v) => v.trim() !== '');

  // Build the frozen-contract corrections array: key is `field` (= field_path), money
  // values converted ₹→integer-paise; blank / unparseable money edits are dropped.
  function buildCorrections(): CnCorrection[] {
    return Object.entries(edits).flatMap(([field, raw]) => {
      const v = raw.trim();
      if (v === '') return [];
      if (isMoneyField(field)) {
        const paise = parseRupeesToPaise(v);
        return paise == null ? [] : [{ field, value: String(paise) }];
      }
      return [{ field, value: v }];
    });
  }

  // Soft over-credit visibility: the CN's credit total exceeds the credited invoice's
  // grand total. NEVER blocks Confirm — the backend intentionally soft-flags.
  const ref = cn.referenced_invoice;
  const creditExceedsInvoice =
    ref?.grand_total_paise != null &&
    cn.grand_total_paise != null &&
    cn.grand_total_paise > ref.grand_total_paise;

  // Confirm gating is unchanged (operate + editable + all-matched + ref-invoice CONFIRMED);
  // a visibly-broken money edit is the only added guard (mirrors the invoice lane). The
  // over-credit tally is a SOFT warning and deliberately does NOT gate.
  const canConfirm =
    canOperate &&
    isEditableStatus &&
    hasLines &&
    allLinesMatched &&
    refInvoiceConfirmed &&
    !hasMoneyError;

  function onConfirm() {
    if (!canConfirm) return;
    submit.mutate(
      { id: cn!.id, corrections: buildCorrections(), confirm: true },
      {
        onSuccess: (updated) => {
          setEdits({});
          if (updated.status === 'CONFIRMED') {
            toast.success('Credit note confirmed.');
          } else {
            toast.info(`Saved — status is now ${CN_STATUS_LABEL[updated.status]}.`);
          }
        },
        // Surface the backend's honest 409 reason VERBATIM (invoice not confirmed /
        // unmatched line).
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function onSaveDraft() {
    if (hasMoneyError) return;
    const corrections = buildCorrections();
    if (corrections.length === 0) return;
    submit.mutate(
      { id: cn!.id, corrections, confirm: false },
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
    rematch.mutate(cn!.id, {
      onSuccess: (updated) => {
        const matched = updated.lines.filter(
          (l) => l.match_status === 'MATCHED' || l.match_status === 'MANUAL',
        ).length;
        toast.info(`Re-matched — ${matched}/${updated.lines.length} line(s) matched.`);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  function onMap(line: CreditNoteLine, poLineItemId: string) {
    setMappingLineId(line.id);
    manualMatch.mutate(
      { id: cn!.id, lineId: line.id, poLineItemId },
      {
        onSuccess: () => toast.success(`Line ${line.line_no} mapped.`),
        onError: (err) => toast.error(errorMessage(err)),
        onSettled: () => setMappingLineId(null),
      },
    );
  }

  function onCancelCn() {
    cancel.mutate(cn!.id, {
      onSuccess: () => {
        setConfirmCancel(false);
        toast.success('Credit note cancelled.');
      },
      onError: (err) => {
        setConfirmCancel(false);
        toast.error(errorMessage(err));
      },
    });
  }

  function onDeleteCn() {
    del.mutate(cn!.id, {
      onSuccess: () => {
        setConfirmDelete(false);
        toast.success('Credit note deleted.');
        navigate(`${BILLING_BASE}/credit-notes`);
      },
      onError: (err) => {
        setConfirmDelete(false);
        toast.error(errorMessage(err));
      },
    });
  }

  const confirmReason = hasMoneyError
    ? 'Fix the highlighted amount to confirm.'
    : !hasLines
      ? 'At least one line item is required to confirm.'
      : !allLinesMatched
        ? 'Match every line to a PO line to confirm.'
        : !refInvoiceConfirmed
          ? 'The credited invoice must be Confirmed before this credit note can be confirmed.'
          : !canOperate
            ? 'You need Operate access to confirm this credit note.'
            : '';

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={cn.cn_number ? `Credit note ${cn.cn_number}` : `Credit note #${cn.id}`}
        subtitle="A credit note reduces the amount receivable against the invoice it references."
        actions={<Badge tone={CN_STATUS_TONE[cn.status]}>{CN_STATUS_LABEL[cn.status]}</Badge>}
      />

      {cn.review_reasons.length > 0 && (
        <div
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          <p className="mb-1 font-semibold">Why this needs a look</p>
          <ul className="list-disc pl-5">
            {cn.review_reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {cn.needs_ocr && (
        <div
          role="status"
          className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600"
        >
          This PDF has no text layer (scanned/image). OCR is not available yet, so its fields could
          not be extracted automatically.
        </div>
      )}

      {/* Referenced invoice header — the CN can only be confirmed once this is CONFIRMED. */}
      <Card className="mb-6 p-5">
        <h2 className="mb-2 text-sm font-semibold text-slate-900">Credited invoice</h2>
        {ref == null ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            This credit note is not linked to an invoice. It cannot be confirmed until it references a
            confirmed invoice.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Invoice</span>
              <Link
                to={`${BILLING_BASE}/${ref.id}`}
                className="text-sm font-medium text-brand-600 hover:text-brand-700"
              >
                {ref.invoice_number ?? `#${ref.id}`}
              </Link>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Invoice status</span>
              <Badge tone={referencedInvoiceTone(ref.status)}>{ref.status}</Badge>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Invoice total</span>
              <span className="tabular-nums text-sm text-slate-900">{money(ref.grand_total_paise)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Credit note date</span>
              <span className="text-sm text-slate-900">{formatDate(cn.cn_date)}</span>
            </div>
          </div>
        )}
        {!refInvoiceConfirmed && ref != null && (
          <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            {ref.status === 'CANCELLED' || ref.status === 'REJECTED'
              ? `The credited invoice is ${ref.status === 'CANCELLED' ? 'Cancelled' : 'Rejected'} — this credit note can no longer be confirmed against it.`
              : 'The credited invoice is not Confirmed yet — a credit note can only be confirmed against a confirmed invoice.'}
          </p>
        )}
      </Card>

      {/* Extracted totals — shown plainly (positive on the wire) but labelled as a credit. */}
      <Card className="mb-6 p-5">
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">Credit note totals</h2>
          <span className="text-xs font-medium uppercase tracking-wide text-amber-700">Credit</span>
        </div>
        <p className="mb-2 text-xs text-slate-500">
          These amounts reduce the receivable against the credited invoice.
        </p>
        <TotalRow label="Total taxable" value={cn.total_taxable_paise} />
        <TotalRow label="Total CGST" value={cn.total_cgst_paise} />
        <TotalRow label="Total SGST" value={cn.total_sgst_paise} />
        <TotalRow label="Total IGST" value={cn.total_igst_paise} />
        <TotalRow label="Round off" value={cn.round_off_paise} signed />
        <div className="flex items-center justify-between border-t-2 border-slate-200 py-2 text-sm font-semibold">
          <span className="text-slate-900">Credit total</span>
          <span className="tabular-nums text-slate-900">{money(cn.grand_total_paise)}</span>
        </div>
        {creditExceedsInvoice && ref != null && (
          <p
            role="status"
            className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800"
          >
            This credit note's total ({money(cn.grand_total_paise)}) exceeds the credited invoice's
            total ({money(ref.grand_total_paise)}). Confirm only if this is intended.
          </p>
        )}
        {cn.reason && (
          <p className="mt-3 text-xs text-slate-500">
            <span className="font-medium text-slate-600">Reason:</span> {cn.reason}
          </p>
        )}
      </Card>

      {/* Field editor — only the header/total fields flagged for review (low-confidence,
          missing, or already corrected). Mirrors the invoice lane's affordance. */}
      {flaggedFields.length > 0 && (
        <Card className="mb-6 p-5">
          <h2 className="mb-1 text-sm font-semibold text-slate-900">Fields flagged for review</h2>
          <p className="mb-2 text-xs text-slate-500">
            Fields the extractor was unsure about are editable. Correct them, then Save corrections
            or Confirm.
          </p>
          {flaggedFields.map((f) => (
            <CnFieldRow
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
          <h2 className="text-sm font-semibold text-slate-900">Line items &amp; PO matching</h2>
          {poId != null && isEditableStatus && (
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
        {poQuery.isError && poId != null && (
          <div className="mb-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Could not load the purchase order's lines — manual matching is unavailable until it loads.
          </div>
        )}
        <LinesMatchTable
          cn={cn}
          poLines={poLines}
          poLoading={poId != null && poQuery.isPending}
          canOperate={canOperate}
          onMap={onMap}
          mappingLineId={mappingLineId}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-slate-200 pt-4">
        {alreadyConfirmed ? (
          <p className="text-sm text-emerald-700">This credit note is confirmed. Its values are frozen.</p>
        ) : isCancelled ? (
          <p className="text-sm text-slate-500">This credit note was cancelled.</p>
        ) : isRejected ? (
          <p className="text-sm text-slate-500">
            This credit note was rejected at the quality gate and cannot be confirmed.
          </p>
        ) : !canOperate ? (
          <p className="text-sm text-slate-500">
            You have view-only access to this credit note — confirming and matching need Operate access.
          </p>
        ) : (
          <>
            <Button onClick={onConfirm} disabled={!canConfirm} loading={submit.isPending}>
              Confirm credit note
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

        {/* Cancel / Delete are MANAGE-only. */}
        {canManage && (
          <div className="ml-auto flex items-center gap-2">
            {!alreadyConfirmed && !isCancelled && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmCancel(true)}
                disabled={cancel.isPending}
              >
                Cancel credit note
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
        title="Cancel this credit note?"
        confirmLabel="Cancel credit note"
        danger
        loading={cancel.isPending}
        onCancel={() => setConfirmCancel(false)}
        onConfirm={onCancelCn}
        message="This soft-cancels the credit note. It stays in the register as Cancelled and cannot be confirmed unless reopened."
      />

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this credit note?"
        confirmLabel="Delete credit note"
        danger
        loading={del.isPending}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={onDeleteCn}
        message="This permanently deletes the credit note and its source PDF. This cannot be undone."
      />
    </div>
  );
}
