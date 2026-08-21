import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  ConfirmDialog,
  PageHeader,
  SelectField,
  StatePanel,
  useToast,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery } from '../../api/projects';
import { usePurchaseOrdersQuery } from '../../api/purchaseOrders';
import {
  useDeleteInvoice,
  useUploadBillingInvoices,
  type BillingUploadOutcome,
} from '../../api/billingInvoices';
import { BILLING_BASE } from './billingFormat';
import {
  UPLOAD_STATUS_LABEL,
  UPLOAD_STATUS_TONE,
  errorMessage,
  money,
} from './billingInvoiceFormat';

/** True when a result links to a stored invoice worth reviewing/opening. */
function hasDetail(r: BillingUploadOutcome): boolean {
  return r.invoice_id != null && r.status !== 'REJECTED';
}

export function InvoiceUpload() {
  const toast = useToast();
  const perms = usePermissions();
  // Deleting an EXISTING invoice on the delete-and-re-upload path needs MANAGE;
  // uploading needs OPERATE. The backend enforces both — these gates set honest
  // expectations. Keep the button enabled while /me is still loading so it doesn't
  // flash disabled for a user who does have OPERATE.
  const canManage = perms.atLeast('billing', 'MANAGE');
  const canUpload = perms.loading || perms.atLeast('billing', 'OPERATE');

  const upload = useUploadBillingInvoices();
  const del = useDeleteInvoice();

  const clientsQuery = useClientsQuery();
  const [clientId, setClientId] = useState('');
  const [poId, setPoId] = useState('');
  // POs for the (optional) PO picker are scoped to the chosen client. The query only
  // matters once a client is picked; until then the select is disabled.
  const posQuery = usePurchaseOrdersQuery({ client_id: clientId });

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  // Outcomes are index-aligned with `files` (one outcome per file, in order), so a
  // row's identity is its position — never its filename, which can collide.
  const [results, setResults] = useState<BillingUploadOutcome[] | null>(null);
  const [dupTarget, setDupTarget] = useState<{ index: number; result: BillingUploadOutcome } | null>(
    null,
  );
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  function reset() {
    setFiles([]);
    setResults(null);
    setDupTarget(null);
    setResolvingId(null);
    upload.reset();
    del.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function onClientChange(value: string) {
    setClientId(value);
    // A PO belongs to exactly one client, so switching client invalidates the PO choice.
    setPoId('');
  }

  function summarize(outcomes: BillingUploadOutcome[]) {
    const dupes = outcomes.filter((r) => r.status === 'DUPLICATE').length;
    const rejected = outcomes.filter((r) => r.status === 'REJECTED').length;
    if (dupes > 0 && rejected > 0) {
      toast.info(
        `${dupes} matched an existing invoice; ${rejected} could not be read. See the list below.`,
      );
    } else if (dupes > 0) {
      toast.info(`${dupes} file${dupes === 1 ? '' : 's'} matched an existing invoice — resolve below.`);
    } else if (rejected > 0) {
      toast.error(
        `${rejected} file${rejected === 1 ? '' : 's'} could not be read — see the list below.`,
      );
    } else {
      toast.success(`Processed ${outcomes.length} file${outcomes.length === 1 ? '' : 's'}.`);
    }
  }

  function onUpload() {
    if (files.length === 0 || !clientId) return;
    upload.mutate(
      { files, clientId, poId: poId || undefined },
      {
        onSuccess: (batch) => {
          setResults(batch.outcomes);
          summarize(batch.outcomes);
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  /**
   * Delete-and-re-upload for a DUPLICATE: delete the existing invoice, then re-submit
   * ONLY the file that collided, and splice its fresh outcome back into the list by
   * index. Any failure leaves the row as-is with a durable toast — no silent success.
   */
  async function confirmReplace() {
    const target = dupTarget;
    if (!target?.result.duplicate_of) return;
    const file = files[target.index];
    if (!file) {
      toast.error('Could not find the original file to re-upload. Choose the files again.');
      setDupTarget(null);
      return;
    }
    setResolvingId(target.result.file_id);
    try {
      await del.mutateAsync(target.result.duplicate_of);
      const batch = await upload.mutateAsync({ files: [file], clientId, poId: poId || undefined });
      const fresh = batch.outcomes[0];
      setResults((prev) => (prev ?? []).map((r, i) => (i === target.index ? fresh : r)));
      setDupTarget(null);
      if (fresh.status === 'DUPLICATE') {
        toast.error('That file still matches an existing invoice.');
      } else if (fresh.status === 'REJECTED') {
        toast.error(`${fresh.filename} could not be read on re-upload.`);
      } else {
        toast.success(`Replaced — ${fresh.filename} re-uploaded.`);
      }
    } catch (err) {
      toast.error(errorMessage(err));
      setDupTarget(null);
    } finally {
      setResolvingId(null);
    }
  }

  const busy = upload.isPending || del.isPending || resolvingId != null;
  const clients = clientsQuery.data ?? [];
  const pos = posQuery.data ?? [];
  const noClients = clientsQuery.isSuccess && clients.length === 0;
  const canSubmit = files.length > 0 && !!clientId;

  return (
    <div>
      <PageHeader
        title="Upload client invoices"
        subtitle="Upload one or more client GST invoice PDFs. Each is extracted, its lines matched to the PO, and added to the register; low-confidence fields and unmatched lines are flagged for review."
      />

      <div className="max-w-2xl">
        <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <SelectField
            label="Client"
            required
            value={clientId}
            onChange={(e) => onClientChange(e.target.value)}
            disabled={clientsQuery.isPending || noClients}
            hint="Which client this batch of invoices is billed to."
          >
            <option value="">
              {clientsQuery.isPending ? 'Loading clients…' : 'Select a client…'}
            </option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.code} — {c.name}
              </option>
            ))}
          </SelectField>
          <SelectField
            label="Purchase order (optional)"
            value={poId}
            onChange={(e) => setPoId(e.target.value)}
            disabled={!clientId || posQuery.isPending}
            hint="Match invoice lines against this PO's open lines."
          >
            <option value="">
              {!clientId
                ? 'Choose a client first'
                : posQuery.isPending
                  ? 'Loading purchase orders…'
                  : pos.length === 0
                    ? 'No purchase orders for this client'
                    : 'No PO (match later)'}
            </option>
            {pos.map((po) => (
              <option key={po.id} value={po.id}>
                {po.po_number}
                {po.project_code ? ` — ${po.project_code}` : ''}
              </option>
            ))}
          </SelectField>
        </div>

        {noClients && (
          <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No clients exist yet. Register one in the Projects module before uploading invoices.
          </p>
        )}

        <label htmlFor="billing-files" className="mb-1 block text-xs font-medium text-slate-600">
          Invoice PDFs (one invoice per file)
        </label>
        <input
          id="billing-files"
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          PDF only. A single multi-invoice PDF is not supported — split it into one file per invoice.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            onClick={onUpload}
            disabled={!canSubmit || busy || !canUpload}
            loading={upload.isPending && resolvingId == null}
          >
            {files.length > 0
              ? `Upload ${files.length} file${files.length === 1 ? '' : 's'}`
              : 'Upload'}
          </Button>
          {(results != null || files.length > 0) && (
            <Button variant="ghost" onClick={reset} disabled={busy}>
              Clear
            </Button>
          )}
          {!busy && (!canSubmit || !canUpload) && (
            <span className="text-xs text-slate-500">
              {!perms.loading && !canUpload
                ? 'You need Operate access to this module to upload.'
                : files.length === 0
                  ? 'Choose at least one PDF to upload.'
                  : 'Select a client to enable upload.'}
            </span>
          )}
        </div>
      </div>

      {results != null && (
        <div className="mt-8 max-w-2xl">
          <h2 className="mb-2 text-sm font-semibold text-slate-900">Results</h2>
          {results.length === 0 ? (
            <StatePanel title="No files processed">Nothing came back for this upload.</StatePanel>
          ) : (
            <ul className="divide-y divide-slate-100 rounded-xl border border-slate-200 bg-white">
              {results.map((r, index) => {
                const isResolving = resolvingId === r.file_id;
                return (
                  <li key={`${r.file_id}-${index}`} className="flex flex-col gap-2 px-4 py-3">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="font-medium text-slate-900">{r.filename}</span>
                      <Badge tone={UPLOAD_STATUS_TONE[r.status]}>
                        {UPLOAD_STATUS_LABEL[r.status]}
                      </Badge>
                      <div className="ml-auto flex items-center gap-2">
                        {r.status === 'DUPLICATE' && r.duplicate_of && (
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => setDupTarget({ index, result: r })}
                            disabled={busy}
                            loading={isResolving}
                          >
                            Resolve duplicate
                          </Button>
                        )}
                        {hasDetail(r) && (
                          <Link
                            to={`${BILLING_BASE}/${r.invoice_id}`}
                            className="text-sm font-medium text-brand-600 hover:text-brand-700"
                          >
                            {r.status === 'REJECTED' ? 'View' : 'Review'}
                          </Link>
                        )}
                      </div>
                    </div>

                    {r.status === 'DUPLICATE' && r.duplicate_of && (
                      <p className="text-xs text-amber-700">
                        Matches existing invoice {r.invoice_number ?? `#${r.duplicate_of}`} (
                        {money(r.grand_total_paise)}).
                      </p>
                    )}

                    {r.review_reasons && r.review_reasons.length > 0 && (
                      <ul className="list-disc pl-5 text-xs text-slate-500">
                        {r.review_reasons.map((reason, i) => (
                          <li key={i}>{reason}</li>
                        ))}
                      </ul>
                    )}

                    {r.status === 'REJECTED' && r.message && (
                      <p className="text-xs text-rose-600">{r.message}</p>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      <ConfirmDialog
        open={dupTarget != null}
        title="Invoice already exists"
        confirmLabel="Delete existing & re-upload"
        danger
        loading={resolvingId != null}
        onCancel={() => {
          if (resolvingId == null) setDupTarget(null);
        }}
        onConfirm={() => void confirmReplace()}
        message={
          dupTarget?.result.duplicate_of ? (
            <div>
              <p>
                An invoice with these values already exists (
                <span className="font-semibold">
                  {dupTarget.result.invoice_number ?? `#${dupTarget.result.duplicate_of}`}
                </span>{' '}
                / <span className="font-semibold">{money(dupTarget.result.grand_total_paise)}</span>
                ). Delete the existing one and re-upload{' '}
                <span className="font-semibold">{dupTarget.result.filename}</span>? This permanently
                removes the existing invoice and cannot be undone.
              </p>
              {!canManage && (
                <p className="mt-2 text-xs text-amber-700">
                  Note: deleting an existing invoice needs the Manage permission — this will fail
                  with a permission error and you'll need someone with Manage access.
                </p>
              )}
            </div>
          ) : null
        }
      />
    </div>
  );
}
