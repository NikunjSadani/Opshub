import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  ConfirmDialog,
  PageHeader,
  StatePanel,
  useToast,
} from '../../ui';
import {
  useDeleteInvoice,
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
  const upload = useUploadInvoices();
  const del = useDeleteInvoice();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [files, setFiles] = useState<File[]>([]);
  const [results, setResults] = useState<UploadResult[] | null>(null);
  // The DUPLICATE row awaiting the delete-and-re-upload confirm (null = closed).
  const [dupTarget, setDupTarget] = useState<UploadResult | null>(null);
  // Which filename is being re-processed, so its row shows progress and the
  // dialog's confirm stays busy across the delete + re-upload round-trip.
  const [resolving, setResolving] = useState<string | null>(null);

  function reset() {
    setFiles([]);
    setResults(null);
    setDupTarget(null);
    setResolving(null);
    upload.reset();
    del.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function onUpload() {
    if (files.length === 0) return;
    upload.mutate(files, {
      onSuccess: (batch) => {
        setResults(batch.outcomes);
        const dupes = batch.outcomes.filter((r) => r.status === 'DUPLICATE').length;
        const rejected = batch.outcomes.filter((r) => r.status === 'REJECTED').length;
        if (dupes > 0) {
          toast.info(
            `${dupes} file${dupes === 1 ? '' : 's'} matched an existing invoice — resolve ${dupes === 1 ? 'it' : 'them'} below.`,
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
    if (!target?.duplicate_of) return;
    const file = files.find((f) => f.name === target.filename);
    if (!file) {
      toast.error('Could not find the original file to re-upload. Choose the files again.');
      setDupTarget(null);
      return;
    }
    setResolving(target.filename);
    try {
      await del.mutateAsync(target.duplicate_of);
      const batch = await upload.mutateAsync([file]);
      const fresh = batch.outcomes[0];
      setResults((prev) =>
        (prev ?? []).map((r) => (r.filename === target.filename ? fresh : r)),
      );
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
      setResolving(null);
    }
  }

  const busy = upload.isPending || del.isPending || resolving != null;

  return (
    <div>
      <PageHeader
        title="Upload invoices"
        subtitle="Upload one or more vendor GST invoice PDFs. Each is extracted and added to the register; low-confidence fields are flagged for review."
      />

      <div className="max-w-2xl">
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

        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={onUpload} disabled={files.length === 0 || busy} loading={upload.isPending && resolving == null}>
            {files.length > 0
              ? `Upload ${files.length} file${files.length === 1 ? '' : 's'}`
              : 'Upload'}
          </Button>
          {(results != null || files.length > 0) && (
            <Button variant="ghost" onClick={reset} disabled={busy}>
              Clear
            </Button>
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
              {results.map((r) => {
                const isResolving = resolving === r.filename;
                return (
                  <li key={r.filename} className="flex flex-col gap-2 px-4 py-3">
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
                            onClick={() => setDupTarget(r)}
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
        loading={resolving != null}
        onCancel={() => {
          if (resolving == null) setDupTarget(null);
        }}
        onConfirm={() => void confirmReplace()}
        message={
          dupTarget?.duplicate_of ? (
            <p>
              An invoice with this number already exists (
              <span className="font-semibold">{dupTarget.invoice_number}</span> /{' '}
              <span className="font-semibold">
                {formatPaise(dupTarget.grand_total_paise)}
              </span>
              ). Delete the existing one and re-upload{' '}
              <span className="font-semibold">{dupTarget.filename}</span>? This permanently removes
              the existing invoice and cannot be undone.
            </p>
          ) : null
        }
      />
    </div>
  );
}
