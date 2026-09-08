import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  PageHeader,
  SearchableSelect,
  StatePanel,
  useToast,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery } from '../../api/projects';
import { useBillingInvoicesQuery } from '../../api/billingInvoices';
import {
  useUploadCreditNotes,
  type CnUploadOutcome,
} from '../../api/billingCreditNotes';
import { BILLING_BASE, rupees } from './billingFormat';
import { errorMessage } from './billingInvoiceFormat';
import { CN_UPLOAD_STATUS_LABEL, CN_UPLOAD_STATUS_TONE } from './creditNoteFormat';

/** True when a result links to a stored credit note worth opening. */
function hasDetail(r: CnUploadOutcome): boolean {
  return r.cn_id != null && r.status !== 'REJECTED';
}

export function CreditNoteUpload() {
  const toast = useToast();
  const perms = usePermissions();
  // Uploading needs OPERATE; keep the button enabled while /me is still loading so it
  // doesn't flash disabled for a user who does have OPERATE (the backend enforces).
  const canUpload = perms.loading || perms.atLeast('billing', 'OPERATE');

  const upload = useUploadCreditNotes();

  // A credit note is issued against a CONFIRMED sales invoice — only those are choosable.
  const invoicesQuery = useBillingInvoicesQuery({ status: 'CONFIRMED' });
  // Load EVERY confirmed-invoice page (not just the first ~100) so the searchable picker can
  // reach any invoice — this is a REQUIRED field, so a missing invoice would hard-block the
  // upload. (Server-side invoice search is the future optimisation if the set grows large.)
  useEffect(() => {
    if (invoicesQuery.hasNextPage && !invoicesQuery.isFetchingNextPage) {
      void invoicesQuery.fetchNextPage();
    }
  }, [invoicesQuery.hasNextPage, invoicesQuery.isFetchingNextPage, invoicesQuery.fetchNextPage]);
  const invoices = useMemo(() => invoicesQuery.data?.pages.flat() ?? [], [invoicesQuery.data]);
  const clientsQuery = useClientsQuery();
  const clientById = useMemo(
    () => new Map((clientsQuery.data ?? []).map((c) => [String(c.id), c])),
    [clientsQuery.data],
  );

  const [invoiceId, setInvoiceId] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  // Outcomes are index-aligned with `files` (one outcome per file, in order), so a
  // row's display name is the local File at that position — never a wire filename.
  const [results, setResults] = useState<CnUploadOutcome[] | null>(null);

  function reset() {
    setFiles([]);
    setResults(null);
    upload.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function summarize(outcomes: CnUploadOutcome[]) {
    const dupes = outcomes.filter((r) => r.status === 'DUPLICATE').length;
    const rejected = outcomes.filter((r) => r.status === 'REJECTED').length;
    if (dupes > 0 && rejected > 0) {
      toast.info(`${dupes} matched an existing credit note; ${rejected} could not be read. See below.`);
    } else if (dupes > 0) {
      toast.info(`${dupes} file${dupes === 1 ? '' : 's'} matched an existing credit note — see below.`);
    } else if (rejected > 0) {
      toast.error(`${rejected} file${rejected === 1 ? '' : 's'} could not be read — see below.`);
    } else {
      toast.success(`Processed ${outcomes.length} file${outcomes.length === 1 ? '' : 's'}.`);
    }
  }

  function onUpload() {
    if (files.length === 0 || !invoiceId) return;
    upload.mutate(
      { files, invoiceId },
      {
        onSuccess: (batch) => {
          setResults(batch.outcomes);
          summarize(batch.outcomes);
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  const busy = upload.isPending;
  const noInvoices = invoicesQuery.isSuccess && invoices.length === 0;
  const canSubmit = files.length > 0 && !!invoiceId;

  const backLink = (
    <Link
      to={`${BILLING_BASE}/credit-notes`}
      className="text-sm font-medium text-brand-600 hover:text-brand-700"
    >
      ← Back to credit notes
    </Link>
  );

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title="Upload credit notes"
        subtitle="Upload one or more client credit-note PDFs against the confirmed invoice they credit. Each is extracted, its lines matched to the invoice's PO, and added to the register; unmatched lines are flagged for review."
      />

      <div className="max-w-2xl">
        <div className="mb-4">
          <SearchableSelect
            label="Credited invoice"
            required
            value={invoiceId}
            onChange={setInvoiceId}
            disabled={invoicesQuery.isPending || invoicesQuery.isError || noInvoices}
            hint="Which CONFIRMED sales invoice this batch of credit notes is issued against."
            placeholder={
              invoicesQuery.isPending
                ? 'Loading invoices…'
                : invoicesQuery.isError
                  ? 'Could not load invoices'
                  : 'Select a confirmed invoice…'
            }
            options={invoices.map((inv) => {
              const client = clientById.get(String(inv.client_id));
              const label = inv.invoice_number ?? `Invoice #${inv.id}`;
              return {
                value: String(inv.id),
                label: `${label}${client ? ` — ${client.code}` : ''}${
                  inv.grand_total_paise != null ? ` (${rupees(inv.grand_total_paise)})` : ''
                }`,
              };
            })}
          />
        </div>

        {invoicesQuery.isError && (
          <p className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
            Couldn&apos;t load invoices.{' '}
            <button
              type="button"
              onClick={() => void invoicesQuery.refetch()}
              className="font-medium underline hover:text-red-900"
            >
              Retry
            </button>
          </p>
        )}

        {noInvoices && (
          <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No confirmed invoices exist yet. A credit note can only be raised against a confirmed
            invoice — confirm one in the Invoices tab first.
          </p>
        )}

        <label htmlFor="cn-files" className="mb-1 block text-xs font-medium text-slate-600">
          Credit-note PDFs (one credit note per file)
        </label>
        <input
          id="cn-files"
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          PDF only. A single multi-CN PDF is not supported — split it into one file per credit note.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            onClick={onUpload}
            disabled={!canSubmit || busy || !canUpload}
            loading={upload.isPending}
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
                  : 'Select the credited invoice to enable upload.'}
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
              {results.map((r, index) => (
                <li key={`${r.file_id}-${index}`} className="flex flex-col gap-2 px-4 py-3">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="font-medium text-slate-900">
                      {files[index]?.name ?? r.cn_number ?? `File #${r.file_id}`}
                    </span>
                    <Badge tone={CN_UPLOAD_STATUS_TONE[r.status]}>
                      {CN_UPLOAD_STATUS_LABEL[r.status]}
                    </Badge>
                    <div className="ml-auto flex items-center gap-2">
                      {hasDetail(r) && (
                        <Link
                          to={`${BILLING_BASE}/credit-notes/${r.cn_id}`}
                          className="text-sm font-medium text-brand-600 hover:text-brand-700"
                        >
                          Review
                        </Link>
                      )}
                    </div>
                  </div>

                  {r.status === 'DUPLICATE' && (
                    <p className="text-xs text-amber-700">
                      Matches an existing credit note
                      {r.cn_number ? ` ${r.cn_number}` : r.cn_id ? ` #${r.cn_id}` : ''}. It was not
                      stored again.
                    </p>
                  )}

                  {r.status === 'REJECTED' && (
                    <p className="text-xs text-rose-600">
                      This document could not be read as a credit note.
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
