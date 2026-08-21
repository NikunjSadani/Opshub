import { useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  StatePanel,
  Table,
  Td,
  TextArea,
  TextField,
  THead,
  Th,
  Tr,
  useToast,
} from '../../ui';
import { ApiError, useApi } from '../../api/client';
import { usePermissions } from '../../auth/AuthProvider';
import {
  LINE_STATUS_LABEL,
  LINE_STATUS_TONE,
  PO_STATUS_LABEL,
  PO_STATUS_TONE,
  useAmendPurchaseOrder,
  useConfirmPurchaseOrder,
  usePurchaseOrderQuery,
  useShortClosePO,
  useVoidPO,
  type LineStatus,
  type PODetail as PODetailType,
  type POStatus,
} from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE, rupees } from './salesOrdersFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

function DefItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-800">{children}</dd>
    </div>
  );
}

export function PODetail() {
  const { id = '' } = useParams();
  const toast = useToast();
  const perms = usePermissions();
  const { download } = useApi();

  const canOperate = perms.atLeast('sales_orders', 'OPERATE');
  const canManage = perms.atLeast('sales_orders', 'MANAGE');

  const query = usePurchaseOrderQuery(id || null);
  const po = query.data;

  if (query.isPending) return <Loading label="Loading purchase order…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!po) return <StatePanel title="Not found">This purchase order does not exist.</StatePanel>;

  return <PODetailBody po={po} canOperate={canOperate} canManage={canManage} download={download} toast={toast} />;
}

function PODetailBody({
  po,
  canOperate,
  canManage,
  download,
  toast,
}: {
  po: PODetailType;
  canOperate: boolean;
  canManage: boolean;
  download: (fileId: number, fallbackName?: string) => Promise<void>;
  toast: ReturnType<typeof useToast>;
}) {
  const amend = useAmendPurchaseOrder();
  const confirmPo = useConfirmPurchaseOrder();
  const shortClose = useShortClosePO();
  const voidPo = useVoidPO();

  const [amendOpen, setAmendOpen] = useState(false);
  // The short-close target: a specific line, or 'PO' for the whole order (null = closed).
  const [scTarget, setScTarget] = useState<{ lineId?: string; label: string } | null>(null);
  const [scReason, setScReason] = useState('');
  const [voidOpen, setVoidOpen] = useState(false);
  const [voidReason, setVoidReason] = useState('');
  // Guard the soft-copy download so a double-click can't fire two downloads.
  const [downloadingSoftCopy, setDownloadingSoftCopy] = useState(false);

  const terminal = po.status === 'CLOSED' || po.status === 'CANCELLED';
  const openLines = po.lines.filter((l) => l.line_status === 'OPEN');

  function submitShortClose() {
    if (!scTarget || scReason.trim() === '') {
      toast.error('A reason is required to short-close.');
      return;
    }
    shortClose.mutate(
      { id: po.id, line_id: scTarget.lineId, reason: scReason.trim() },
      {
        onSuccess: () => {
          toast.success('Short-closed.');
          setScTarget(null);
          setScReason('');
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function submitConfirm() {
    confirmPo.mutate(
      { id: po.id },
      {
        onSuccess: () => toast.success('Purchase order confirmed.'),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function submitVoid() {
    if (voidReason.trim() === '') {
      toast.error('A reason is required to void a purchase order.');
      return;
    }
    voidPo.mutate(
      { id: po.id, reason: voidReason.trim() },
      {
        onSuccess: () => {
          toast.success('Purchase order voided.');
          setVoidOpen(false);
          setVoidReason('');
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <div>
      <PageHeader
        title={`PO ${po.po_number}`}
        subtitle={po.client_name ?? undefined}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to={SALES_ORDERS_BASE}
              className="text-sm font-medium text-slate-500 hover:text-slate-700"
            >
              Back to register
            </Link>
            {/* Confirm — OPERATE, only while the PO is still a DRAFT. Non-destructive:
                a single click (no ConfirmDialog). Server 422s if it isn't DRAFT. */}
            {canOperate && po.status === 'DRAFT' && (
              <Button
                variant="primary"
                size="sm"
                onClick={submitConfirm}
                loading={confirmPo.isPending}
              >
                Confirm PO
              </Button>
            )}
            {/* Amend is OPERATE and only while the PO is still amendable. */}
            {canOperate && !terminal && (
              <Button variant="secondary" size="sm" onClick={() => setAmendOpen(true)}>
                Amend
              </Button>
            )}
            {/* Short-close the whole PO — MANAGE, and only with open lines left. */}
            {canManage && !terminal && openLines.length > 0 && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setScReason('');
                  setScTarget({ label: `whole PO ${po.po_number}` });
                }}
              >
                Short-close PO
              </Button>
            )}
            {/* Void — MANAGE, destructive-ish (soft cancel), confirm + reason. */}
            {canManage && !terminal && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => {
                  setVoidReason('');
                  setVoidOpen(true);
                }}
              >
                Void
              </Button>
            )}
          </div>
        }
      />

      <div className="mb-6 rounded-xl border border-slate-200 bg-white p-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <DefItem label="Status">
            <Badge tone={PO_STATUS_TONE[po.status as POStatus]}>
              {PO_STATUS_LABEL[po.status as POStatus] ?? po.status}
            </Badge>
          </DefItem>
          <DefItem label="Client">{po.client_name ?? '—'}</DefItem>
          <DefItem label="Project">{po.project_code ?? '—'}</DefItem>
          <DefItem label="PO date">{formatDate(po.po_date)}</DefItem>
          <DefItem label="Expected procurement">
            {formatDate(po.expected_procurement_date)}
          </DefItem>
          <DefItem label="Client GSTIN">{po.client_gstin ?? '—'}</DefItem>
          <DefItem label="Lines">{po.line_count}</DefItem>
          <DefItem label="Total sell">{rupees(po.total_sell_paise)}</DefItem>
          <DefItem label="Amendments">{po.amendments_count}</DefItem>
          <DefItem label="Soft copy">
            {po.soft_copy_file ? (
              <button
                type="button"
                disabled={downloadingSoftCopy}
                onClick={() => {
                  if (downloadingSoftCopy) return;
                  setDownloadingSoftCopy(true);
                  void download(Number(po.soft_copy_file!.id), po.soft_copy_file!.filename)
                    .catch((err) => toast.error(errorMessage(err)))
                    .finally(() => setDownloadingSoftCopy(false));
                }}
                className="font-medium text-brand-600 hover:text-brand-700 disabled:opacity-50"
              >
                {downloadingSoftCopy ? 'Downloading…' : po.soft_copy_file.filename}
              </button>
            ) : (
              '—'
            )}
          </DefItem>
        </dl>
        {po.notes && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">Notes</dt>
            <dd className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700">{po.notes}</dd>
          </div>
        )}
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Line items</h2>
      {po.lines.length === 0 ? (
        <StatePanel title="No lines">This purchase order has no line items.</StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Product</Th>
              <Th>Description</Th>
              <Th>UOM</Th>
              <Th className="text-right">Qty</Th>
              <Th className="text-right">Cost</Th>
              <Th className="text-right">Sell</Th>
              <Th className="text-right">Freight</Th>
              <Th className="text-right">Packaging</Th>
              <Th className="text-right">Handling</Th>
              <Th className="text-right">Other</Th>
              <Th className="text-right">Tax %</Th>
              <Th>Status</Th>
              {canManage && !terminal && <Th className="text-right">Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {po.lines.map((line) => (
              <Tr key={line.id}>
                <Td className="font-medium text-slate-900">
                  {line.product_name ?? `#${line.product_id}`}
                  {line.brand ? <span className="text-slate-400"> · {line.brand}</span> : null}
                </Td>
                <Td>{line.description || '—'}</Td>
                <Td>{line.uom || '—'}</Td>
                <Td className="text-right tabular-nums">{line.ordered_qty}</Td>
                <Td className="text-right tabular-nums">{rupees(line.cost_price_paise)}</Td>
                <Td className="text-right tabular-nums">{rupees(line.sell_price_paise)}</Td>
                <Td className="text-right tabular-nums text-slate-500">
                  {rupees(line.freight_paise)}
                </Td>
                <Td className="text-right tabular-nums text-slate-500">
                  {rupees(line.packaging_paise)}
                </Td>
                <Td className="text-right tabular-nums text-slate-500">
                  {rupees(line.handling_paise)}
                </Td>
                <Td className="text-right tabular-nums text-slate-500">
                  {rupees(line.other_paise)}
                </Td>
                <Td className="text-right tabular-nums">{line.tax_rate}</Td>
                <Td>
                  <Badge tone={LINE_STATUS_TONE[line.line_status as LineStatus]}>
                    {LINE_STATUS_LABEL[line.line_status as LineStatus] ?? line.line_status}
                  </Badge>
                  {line.line_status === 'SHORT_CLOSED' && line.short_close_reason && (
                    <div className="mt-0.5 text-xs text-slate-400">{line.short_close_reason}</div>
                  )}
                </Td>
                {canManage && !terminal && (
                  <Td className="text-right">
                    {line.line_status === 'OPEN' && (
                      <button
                        type="button"
                        onClick={() => {
                          setScReason('');
                          setScTarget({
                            lineId: line.id,
                            label: line.product_name ?? `line #${line.id}`,
                          });
                        }}
                        className="text-sm font-medium text-amber-700 hover:text-amber-800"
                      >
                        Short-close
                      </button>
                    )}
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {amendOpen && (
        <AmendModal
          po={po}
          onClose={() => setAmendOpen(false)}
          busy={amend.isPending}
          onSubmit={(body) =>
            amend.mutate(
              { id: po.id, body },
              {
                onSuccess: () => {
                  toast.success('Purchase order amended.');
                  setAmendOpen(false);
                },
                onError: (err) => toast.error(errorMessage(err)),
              },
            )
          }
        />
      )}

      {/* Short-close: a reason is required (validated on confirm). */}
      <ConfirmDialog
        open={scTarget != null}
        title="Short-close"
        confirmLabel="Short-close"
        loading={shortClose.isPending}
        onCancel={() => {
          if (!shortClose.isPending) {
            setScTarget(null);
            setScReason('');
          }
        }}
        onConfirm={submitShortClose}
        message={
          <div>
            <p className="mb-2">
              Short-close {scTarget?.label}. Remaining quantity will be closed out. This cannot be
              undone.
            </p>
            <TextArea
              label="Reason"
              required
              value={scReason}
              onChange={(e) => setScReason(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="Why is this being short-closed?"
            />
          </div>
        }
      />

      {/* Void: destructive, ConfirmDialog + required reason. */}
      <ConfirmDialog
        open={voidOpen}
        title="Void purchase order"
        confirmLabel="Void purchase order"
        danger
        loading={voidPo.isPending}
        onCancel={() => {
          if (!voidPo.isPending) {
            setVoidOpen(false);
            setVoidReason('');
          }
        }}
        onConfirm={submitVoid}
        message={
          <div>
            <p className="mb-2">
              Void PO <span className="font-semibold">{po.po_number}</span>? It is soft-cancelled
              (nothing is deleted) but can no longer be amended or short-closed.
            </p>
            <TextArea
              label="Reason"
              required
              value={voidReason}
              onChange={(e) => setVoidReason(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="Why is this being voided?"
            />
          </div>
        }
      />
    </div>
  );
}

/** Header-only amend form in a modal (line items are left unchanged). */
function AmendModal({
  po,
  onClose,
  busy,
  onSubmit,
}: {
  po: PODetailType;
  onClose: () => void;
  busy: boolean;
  onSubmit: (body: {
    po_number?: string;
    po_date?: string;
    expected_procurement_date?: string | null;
    notes?: string | null;
    soft_copy_file_id?: string | null;
    summary?: string;
  }) => void;
}) {
  const toast = useToast();
  const { postForm } = useApi();
  const [poNumber, setPoNumber] = useState(po.po_number);
  const [poDate, setPoDate] = useState(po.po_date);
  const [expectedDate, setExpectedDate] = useState(po.expected_procurement_date ?? '');
  const [notes, setNotes] = useState(po.notes ?? '');
  const [summary, setSummary] = useState('');
  // Soft copy: seeded from the PO's current attachment. Removing it clears the link
  // on save (amend sends soft_copy_file_id: null); attaching uploads then links a new id.
  const [softCopy, setSoftCopy] = useState<{ id: string; filename: string } | null>(
    po.soft_copy_file ? { id: String(po.soft_copy_file.id), filename: po.soft_copy_file.filename } : null,
  );
  const [uploadingSoftCopy, setUploadingSoftCopy] = useState(false);

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

  const valid = poNumber.trim() !== '' && poDate !== '' && !uploadingSoftCopy;

  return (
    <Modal
      open
      title={`Amend PO ${po.po_number}`}
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={() =>
              onSubmit({
                po_number: poNumber.trim(),
                po_date: poDate,
                expected_procurement_date: expectedDate || null,
                notes: notes.trim() || null,
                soft_copy_file_id: softCopy?.id ?? null,
                summary: summary.trim() || undefined,
              })
            }
            disabled={!valid}
            loading={busy}
          >
            Save amendment
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-500">
          Editing the PO header. Line items are unchanged by this form.
        </p>
        <TextField
          label="PO number"
          required
          value={poNumber}
          onChange={(e) => setPoNumber(e.target.value)}
          maxLength={64}
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField
            label="PO date"
            type="date"
            required
            value={poDate}
            onChange={(e) => setPoDate(e.target.value)}
          />
          <TextField
            label="Expected procurement date"
            type="date"
            value={expectedDate}
            onChange={(e) => setExpectedDate(e.target.value)}
            min={poDate || undefined}
          />
        </div>
        <TextArea
          label="Notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          maxLength={1000}
        />
        <div>
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
                type="file"
                disabled={uploadingSoftCopy}
                onChange={(e) => void onSoftCopyChange(e.target.files)}
                className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100 disabled:opacity-50"
              />
              {uploadingSoftCopy && <span className="text-xs text-slate-500">Uploading…</span>}
            </div>
          )}
        </div>
        <TextField
          label="Amendment summary (optional)"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          maxLength={500}
          hint="A short note on what changed and why."
        />
      </div>
    </Modal>
  );
}
