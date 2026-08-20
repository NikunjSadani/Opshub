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
import { useProjectsQuery } from '../../api/projects';
import {
  useDeleteInvoice,
  usePaymentMethods,
  useUploadInvoices,
  type UploadResult,
} from '../../api/expense';
import {
  EXPENSE_BASE,
  UPLOAD_STATUS_LABEL,
  UPLOAD_STATUS_TONE,
  errorMessage,
  formatPaise,
} from './expenseFormat';

/** True when a result links to a stored invoice worth reviewing/opening. */
function hasDetail(r: UploadResult): boolean {
  return r.invoice_id != null && r.status !== 'REJECTED';
}

export function Upload() {
  const toast = useToast();
  const perms = usePermissions();
  // Deleting an ALREADY-CONFIRMED invoice (as the delete-and-re-upload path can
  // require) needs MANAGE on the module; uploading needs OPERATE. The backend
  // enforces both — these gates just set honest expectations in the UI.
  const canManage = perms.atLeast('expense_invoice', 'MANAGE');
  // Keep the upload control enabled while permissions are still loading (so we
  // don't flash a disabled button); once /me resolves, a View-only user without
  // OPERATE sees it disabled rather than clicking through to a 403.
  const canUpload = perms.loading || perms.atLeast('expense_invoice', 'OPERATE');
  const upload = useUploadInvoices();
  const del = useDeleteInvoice();
  // Cost-allocation: every invoice in a batch is tagged with the SAME project +
  // payment method, chosen once here. Only ACTIVE projects and active payment
  // methods are choosable (the backend still enforces both are required + valid).
  const projectsQuery = useProjectsQuery({ status: 'ACTIVE' });
  const methodsQuery = usePaymentMethods(true);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [projectId, setProjectId] = useState('');
  const [paymentMethodId, setPaymentMethodId] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  // Outcomes are index-aligned with `files` (the backend returns one outcome per
  // uploaded file, in order), so a row's identity is its position — never its
  // filename, which can collide across two same-named PDFs.
  const [results, setResults] = useState<UploadResult[] | null>(null);
  // The DUPLICATE row awaiting the delete-and-re-upload confirm, with its index so
  // the correct source File is re-uploaded and only that row is spliced (null = closed).
  const [dupTarget, setDupTarget] = useState<{ index: number; result: UploadResult } | null>(null);
  // Which file_id is being re-processed, so its row shows progress and the dialog's
  // confirm stays busy across the delete + re-upload round-trip. Keyed by the stable
  // file_id, not the filename (duplicates would otherwise all show as resolving).
  const [resolvingId, setResolvingId] = useState<number | null>(null);

  function reset() {
    setFiles([]);
    setResults(null);
    setDupTarget(null);
    setResolvingId(null);
    upload.reset();
    del.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function onUpload() {
    if (files.length === 0 || !projectId || !paymentMethodId) return;
    upload.mutate({ files, projectId, paymentMethodId }, {
      onSuccess: (batch) => {
        setResults(batch.outcomes);
        const dupes = batch.outcomes.filter((r) => r.status === 'DUPLICATE').length;
        const rejected = batch.outcomes.filter((r) => r.status === 'REJECTED').length;
        const dupPhrase = `${dupes} file${dupes === 1 ? '' : 's'} matched an existing invoice`;
        const rejPhrase = `${rejected} could not be read`;
        if (dupes > 0 && rejected > 0) {
          // Summarize BOTH problems so a mixed batch doesn't hide its rejects
          // behind the duplicates (or vice-versa) — see the list below for each.
          toast.info(`${dupPhrase}; ${rejPhrase}. See the list below.`);
        } else if (dupes > 0) {
          toast.info(
            `${dupPhrase} — resolve ${dupes === 1 ? 'it' : 'them'} below.`,
          );
        } else if (rejected > 0) {
          toast.error(
            `${rejected} file${rejected === 1 ? '' : 's'} could not be read — see the list below.`,
          );
        } else {
          toast.success(`Processed ${batch.outcomes.length} file${batch.outcomes.length === 1 ? '' : 's'}.`);
        }
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  /**
   * Delete-and-re-upload for a DUPLICATE: delete the existing invoice, then
   * re-submit ONLY the file that collided, and splice its fresh outcome back into
   * the results list. Any failure leaves the row as-is with a durable toast — no
   * silent success.
   */
  async function confirmReplace() {
    const target = dupTarget;
    if (!target?.result.duplicate_of) return;
    // Pair the re-upload to its source File by INDEX (not filename), so two files
    // named the same don't re-upload the wrong blob.
    const file = files[target.index];
    if (!file) {
      toast.error('Could not find the original file to re-upload. Choose the files again.');
      setDupTarget(null);
      return;
    }
    setResolvingId(target.result.file_id);
    try {
      await del.mutateAsync(target.result.duplicate_of);
      // Re-upload with the SAME batch tags so the replacement keeps its allocation.
      const batch = await upload.mutateAsync({ files: [file], projectId, paymentMethodId });
      const fresh = batch.outcomes[0];
      // Splice by index so only the resolved row is replaced (same-named rows stay put).
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

  const projects = projectsQuery.data ?? [];
  const methods = methodsQuery.data ?? [];
  const noProjects = projectsQuery.isSuccess && projects.length === 0;
  const noMethods = methodsQuery.isSuccess && methods.length === 0;
  // Both cost-allocation tags are REQUIRED before an upload can proceed.
  const canSubmit = files.length > 0 && !!projectId && !!paymentMethodId;

  return (
    <div>
      <PageHeader
        title="Upload invoices"
        subtitle="Upload one or more vendor GST invoice PDFs. Each is extracted and added to the register; low-confidence fields are flagged for review."
      />

      <div className="max-w-2xl">
        <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <SelectField
            label="Project"
            required
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={projectsQuery.isPending || noProjects}
            hint="Which project this batch of invoices is charged to."
          >
            <option value="">
              {projectsQuery.isPending ? 'Loading projects…' : 'Select a project…'}
            </option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.code} — {p.name}
              </option>
            ))}
          </SelectField>
          <SelectField
            label="Payment method"
            required
            value={paymentMethodId}
            onChange={(e) => setPaymentMethodId(e.target.value)}
            disabled={methodsQuery.isPending || noMethods}
            hint="How this batch was (or will be) paid."
          >
            <option value="">
              {methodsQuery.isPending ? 'Loading payment methods…' : 'Select a payment method…'}
            </option>
            {methods.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </SelectField>
        </div>

        {noProjects && (
          <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No active projects exist yet. Create one in the Projects module before uploading
            invoices.
          </p>
        )}
        {noMethods && (
          <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No payment methods have been set up yet.{' '}
            {canManage ? (
              <Link
                to={`${EXPENSE_BASE}/payment-methods`}
                className="font-medium text-brand-700 underline hover:text-brand-800"
              >
                Add one on the Payment Methods tab
              </Link>
            ) : (
              'Ask an administrator to add one before uploading invoices.'
            )}
          </p>
        )}

        <label htmlFor="expense-files" className="mb-1 block text-xs font-medium text-slate-600">
          Invoice PDFs (one invoice per file)
        </label>
        <input
          id="expense-files"
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          PDF only. A single multi-invoice PDF is not supported yet — split it into one file per
          invoice.
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
          {files.length > 0 && (!projectId || !paymentMethodId) && (
            <span className="text-xs text-slate-400">
              Select a project and payment method to enable upload.
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
                  <li key={r.file_id} className="flex flex-col gap-2 px-4 py-3">
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
                            to={`${EXPENSE_BASE}/invoices/${r.invoice_id}`}
                            className="text-sm font-medium text-brand-600 hover:text-brand-700"
                          >
                            {r.status === 'NEEDS_REVIEW' ? 'Review' : 'View'}
                          </Link>
                        )}
                      </div>
                    </div>

                    {r.status === 'DUPLICATE' && r.duplicate_of && (
                      <p className="text-xs text-amber-700">
                        Matches existing invoice {r.invoice_number} (
                        {formatPaise(r.grand_total_paise)}).
                      </p>
                    )}

                    {r.review_reasons && r.review_reasons.length > 0 && (
                      <ul className="list-disc pl-5 text-xs text-slate-500">
                        {r.review_reasons.map((reason, i) => (
                          <li key={i}>{reason}</li>
                        ))}
                      </ul>
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
                An invoice with this number already exists (
                <span className="font-semibold">{dupTarget.result.invoice_number}</span> /{' '}
                <span className="font-semibold">
                  {formatPaise(dupTarget.result.grand_total_paise)}
                </span>
                ). Delete the existing one and re-upload{' '}
                <span className="font-semibold">{dupTarget.result.filename}</span>? This permanently
                removes the existing invoice and cannot be undone.
              </p>
              {!canManage && (
                <p className="mt-2 text-xs text-amber-700">
                  Note: if the existing invoice has already been confirmed, removing it needs the
                  Manage permission — this will fail with a permission error and you'll need someone
                  with Manage access to delete it.
                </p>
              )}
            </div>
          ) : null
        }
      />
    </div>
  );
}
