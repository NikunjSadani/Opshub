import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  Badge,
  Button,
  Card,
  ErrorState,
  Loading,
  PageHeader,
  Spinner,
  StatePanel,
  TextField,
  useToast,
} from '../../ui';
import { useApi } from '../../api/client';
import {
  useBatchesQuery,
  useBatchQuery,
  useGenerateBatch,
  useUploadBatch,
  type BatchOut,
} from '../../api/challan';
import { BATCH_STATUS_TONE, batchArtifacts, errorMessage } from './challanFormat';

const SERIES_RE = /^[A-Za-z0-9]{1,8}$/;
const BATCHES_TAB = '/m/document_automation/batches';

/**
 * A numbered, always-visible step card. Later steps stay visibly `disabled` with
 * a short `disabledReason` until their precondition is met, so the flow reads as
 * "do 1, then 2, then 3" and a locked step always explains itself.
 */
function StepCard({
  n,
  title,
  disabled = false,
  disabledReason,
  children,
}: {
  n: number;
  title: string;
  disabled?: boolean;
  disabledReason?: string;
  children: ReactNode;
}) {
  return (
    <Card className={`p-5 ${disabled ? 'opacity-70' : ''}`}>
      <div className="mb-3 flex items-center gap-2.5">
        <span
          className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-sm font-semibold ${
            disabled ? 'bg-slate-100 text-slate-400' : 'bg-brand-600 text-white'
          }`}
        >
          {n}
        </span>
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      </div>
      {disabled && disabledReason ? (
        <p className="text-sm text-slate-400">{disabledReason}</p>
      ) : (
        children
      )}
    </Card>
  );
}

export function NewChallan() {
  const toast = useToast();
  const { download, downloadUrl } = useApi();
  const qc = useQueryClient();

  const [file, setFile] = useState<File | null>(null);
  const [uploaded, setUploaded] = useState<BatchOut | null>(null);
  const [series, setSeries] = useState('L');
  const [activeBatchId, setActiveBatchId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const upload = useUploadBatch();
  const generate = useGenerateBatch();
  // Live batch status while (and after) generating. `data` supersedes `uploaded`.
  const batchQuery = useBatchQuery(activeBatchId);
  const batch = batchQuery.data;
  // Recent batches — refetches automatically after an upload/generate (both
  // mutations invalidate ['challan','batches']).
  const recent = useBatchesQuery(5);

  const seriesValid = SERIES_RE.test(series);
  const validated = uploaded?.status === 'VALIDATED';

  // Surface a "taking longer than expected" hint if generation stays in flight,
  // so a wedged/slow batch never traps the user on a bare spinner.
  const [stalled, setStalled] = useState(false);
  const generating = activeBatchId != null && (!batch || batch.status === 'GENERATING');
  useEffect(() => {
    if (!generating) {
      setStalled(false);
      return;
    }
    const t = window.setTimeout(() => setStalled(true), 60_000);
    return () => window.clearTimeout(t);
  }, [generating]);

  // When the polled batch reaches a terminal status, refresh the recent-batches
  // list so its row can't stay stuck at GENERATING while Step 3 shows COMPLETED.
  useEffect(() => {
    if (batch && (batch.status === 'COMPLETED' || batch.status === 'FAILED')) {
      void qc.invalidateQueries({ queryKey: ['challan', 'batches'] });
    }
  }, [batch?.status, qc]);

  function reset() {
    setFile(null);
    setUploaded(null);
    setSeries('L');
    setActiveBatchId(null);
    setStalled(false);
    upload.reset();
    generate.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  async function onDownloadTemplate(): Promise<void> {
    try {
      await downloadUrl('/challan/template.xlsx', 'challan-upload-template.xlsx');
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  function onUpload() {
    if (!file) return;
    upload.mutate(file, {
      onSuccess: (result) => {
        setUploaded(result);
        if (result.status === 'FAILED_VALIDATION') {
          toast.error('Validation failed — download the error report for details.');
        } else {
          toast.success(`Validated: ${result.challan_count} challans, ${result.line_count} lines.`);
        }
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  function onGenerate() {
    if (!uploaded || !seriesValid) return;
    generate.mutate(
      { batchId: uploaded.id, series },
      {
        onSuccess: (result) => setActiveBatchId(result.id),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  async function onDownload(fileId: number | null, fallback: string) {
    if (fileId == null) return;
    try {
      await download(fileId, fallback);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  const step3Reason =
    uploaded?.status === 'FAILED_VALIDATION'
      ? 'Fix the errors and upload a valid file first.'
      : 'Upload and validate a file first.';

  // A single stable polite live region announces each phase to screen readers
  // (the element the user acted on unmounts on transition, so focus alone won't).
  const liveMessage =
    batch?.status === 'COMPLETED'
      ? `Done. ${batch.challan_count} challan${batch.challan_count === 1 ? '' : 's'} issued.`
      : batch?.status === 'FAILED'
        ? 'Generation failed.'
        : generating
          ? 'Generating challans…'
          : uploaded?.status === 'VALIDATED'
            ? uploaded.error_report_file_id != null
              ? `Validated with warnings: ${uploaded.message ?? 'review the warnings report'}. These are non-blocking — you can still generate.`
              : `Validated: ${uploaded.challan_count} challans, ${uploaded.line_count} lines.`
            : uploaded?.status === 'FAILED_VALIDATION'
              ? 'Validation failed. Download the error report for details.'
              : '';

  return (
    <div>
      <p className="sr-only" role="status" aria-live="polite">
        {liveMessage}
      </p>
      <PageHeader
        title="New Challan"
        subtitle="Get the template, upload the filled workbook, then number and generate the challans."
      />

      <div className="max-w-2xl space-y-4">
        {/* Step 1 — template ------------------------------------------------ */}
        <StepCard n={1} title="Get the template">
          <p className="text-sm text-slate-600">
            Fill the &ldquo;Challans&rdquo; sheet; the &ldquo;Instructions&rdquo; sheet explains
            every column.
          </p>
          <div className="mt-4">
            <Button variant="secondary" onClick={() => void onDownloadTemplate()}>
              Download template
            </Button>
          </div>
        </StepCard>

        {/* Step 2 — upload & validate -------------------------------------- */}
        <StepCard n={2} title="Upload the filled file">
          {uploaded == null && (
            <>
              <label
                htmlFor="challan-file"
                className="mb-1 block text-xs font-medium text-slate-600"
              >
                Excel workbook (.xlsx)
              </label>
              <input
                id="challan-file"
                ref={fileInputRef}
                type="file"
                accept=".xlsx"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
              />
              <p className="mt-1 text-xs text-slate-400">
                A single .xlsx matching the template columns. It is validated immediately on upload.
              </p>
              <div className="mt-4">
                <Button onClick={onUpload} disabled={!file} loading={upload.isPending}>
                  Upload &amp; validate
                </Button>
              </div>
            </>
          )}

          {uploaded?.status === 'VALIDATED' && (
            <>
              <div className="mb-3 flex items-center gap-2">
                <Badge tone="green">Validated</Badge>
                {uploaded.error_report_file_id != null && (
                  <Badge tone="amber">{uploaded.message ?? 'Review warnings'}</Badge>
                )}
                <span className="text-sm text-slate-500">Batch #{uploaded.id}</span>
              </div>
              <dl className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <dt className="text-slate-500">Challans</dt>
                  <dd className="text-lg font-semibold text-slate-900">{uploaded.challan_count}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Line items</dt>
                  <dd className="text-lg font-semibold text-slate-900">{uploaded.line_count}</dd>
                </div>
              </dl>

              {uploaded.error_report_file_id != null && (
                <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3">
                  <p className="mb-1 text-sm font-semibold text-amber-800">
                    Review before generating
                  </p>
                  <p className="text-sm text-amber-800">
                    Non-blocking, but worth a look: where a consignee GSTIN already exists, the
                    stored name/address is printed on the challan — not what you typed. Warnings
                    also flag challans that may be duplicates. Download the report to see each one.
                  </p>
                  <div className="mt-3">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() =>
                        onDownload(
                          uploaded.error_report_file_id,
                          `batch-${uploaded.id}-warnings.csv`,
                        )
                      }
                    >
                      Download warnings report
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}

          {uploaded?.status === 'FAILED_VALIDATION' && (
            <>
              <div className="mb-2 flex items-center gap-2">
                <Badge tone="red">Validation failed</Badge>
                <span className="text-sm text-slate-500">Batch #{uploaded.id}</span>
              </div>
              <p className="text-sm text-slate-600">
                {uploaded.message ?? 'The workbook has validation errors. Fix them and re-upload.'}
              </p>
              <p className="mt-1 text-xs text-slate-400">
                The report lists every problem as Row, Severity, Column, Problem — fix those rows
                and upload again.
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button
                  onClick={() =>
                    onDownload(uploaded.error_report_file_id, `batch-${uploaded.id}-errors.csv`)
                  }
                  disabled={uploaded.error_report_file_id == null}
                >
                  Download error report
                </Button>
                <Button variant="ghost" onClick={reset}>
                  Choose another file
                </Button>
              </div>
            </>
          )}

          {/* Safety net: never leave an unexpected status as a blank dead-end. */}
          {uploaded != null &&
            uploaded.status !== 'VALIDATED' &&
            uploaded.status !== 'FAILED_VALIDATION' && (
              <div>
                <p className="text-sm text-slate-600">
                  Unexpected batch status ({uploaded.status}).
                </p>
                <div className="mt-3">
                  <Button variant="ghost" onClick={reset}>
                    Start over
                  </Button>
                </div>
              </div>
            )}
        </StepCard>

        {/* Step 3 — number & generate -------------------------------------- */}
        <StepCard n={3} title="Number & generate" disabled={!validated} disabledReason={step3Reason}>
          {/* Validated, not yet generating — series + Generate. */}
          {validated && activeBatchId == null && (
            <>
              <div className="max-w-[10rem]">
                <TextField
                  label="Series"
                  value={series}
                  onChange={(e) => setSeries(e.target.value)}
                  error={
                    !seriesValid ? (series ? '1–8 letters/digits' : 'Series is required') : undefined
                  }
                  hint="Number prefix, e.g. L"
                  maxLength={8}
                />
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button onClick={onGenerate} disabled={!seriesValid} loading={generate.isPending}>
                  Generate {uploaded!.challan_count} challan{uploaded!.challan_count === 1 ? '' : 's'}
                  {uploaded!.error_report_file_id != null ? ' (warnings)' : ''}
                </Button>
                <Button variant="ghost" onClick={reset} disabled={generate.isPending}>
                  Cancel
                </Button>
              </div>
            </>
          )}

          {/* Generation phase — poll the batch until terminal. */}
          {activeBatchId != null && (
            <>
              <div className="mb-3 flex items-center gap-2">
                <Badge tone={batch ? BATCH_STATUS_TONE[batch.status] : 'amber'}>
                  {batch?.status ?? 'GENERATING'}
                </Badge>
                <span className="text-sm text-slate-500">Batch #{activeBatchId}</span>
              </div>

              {(!batch || batch.status === 'GENERATING') && (
                <>
                  <div className="flex items-center gap-2 text-sm text-slate-600">
                    <Spinner /> Generating challans — rendering PDFs in the background…
                  </div>
                  {stalled && (
                    <p className="mt-2 text-sm text-amber-700">
                      This is taking longer than expected. You can keep this open, or come back to
                      the Batches tab later — the batch will finish and its artifacts appear there.
                    </p>
                  )}
                  <div className="mt-4">
                    <Button variant="ghost" onClick={reset}>
                      Start over
                    </Button>
                  </div>
                </>
              )}

              {batch?.status === 'COMPLETED' && (
                <>
                  <p className="text-sm text-slate-600">
                    Done — {batch.challan_count} challan{batch.challan_count === 1 ? '' : 's'} issued.
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button
                      onClick={() => onDownload(batch.zip_file_id, `batch-${batch.id}.zip`)}
                      disabled={batch.zip_file_id == null}
                    >
                      Download ZIP
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => onDownload(batch.merged_pdf_file_id, `batch-${batch.id}.pdf`)}
                      disabled={batch.merged_pdf_file_id == null}
                    >
                      Download merged PDF
                    </Button>
                    <Button variant="ghost" onClick={reset}>
                      New upload
                    </Button>
                  </div>
                </>
              )}

              {batch?.status === 'FAILED' && (
                <>
                  <p className="text-sm text-rose-600">{batch.message ?? 'Generation failed.'}</p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button onClick={onGenerate} loading={generate.isPending} disabled={!seriesValid}>
                      Retry generation
                    </Button>
                    <Button variant="ghost" onClick={reset} disabled={generate.isPending}>
                      Start over
                    </Button>
                  </div>
                </>
              )}

              {batchQuery.isError && !batch && (
                <StatePanel tone="red" title="Lost track of this batch">
                  <p>{errorMessage(batchQuery.error)}</p>
                  <button
                    type="button"
                    onClick={() => void batchQuery.refetch()}
                    className="mt-2 text-sm font-medium text-brand-600 hover:text-brand-700"
                  >
                    Check again
                  </button>
                </StatePanel>
              )}

              {/* Safety net: an unexpected (non-terminal-known) status is never a dead-end. */}
              {batch &&
                batch.status !== 'GENERATING' &&
                batch.status !== 'COMPLETED' &&
                batch.status !== 'FAILED' && (
                  <div className="mt-4">
                    <Button variant="ghost" onClick={reset}>
                      Start over
                    </Button>
                  </div>
                )}
            </>
          )}
        </StepCard>
      </div>

      {/* Recent batches --------------------------------------------------- */}
      <div className="mt-8 max-w-2xl">
        <div className="mb-2 flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Recent batches</h2>
          <Link
            to={BATCHES_TAB}
            className="text-xs font-medium text-brand-600 hover:text-brand-700"
          >
            View all batches
          </Link>
        </div>

        {recent.isPending ? (
          <Loading label="Loading batches…" />
        ) : recent.isError ? (
          <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
        ) : recent.data.length === 0 ? (
          <StatePanel title="No batches yet">
            Upload a workbook above to get started.
          </StatePanel>
        ) : (
          <Card className="divide-y divide-slate-100 px-4">
            {recent.data.map((b) => {
              const links = batchArtifacts(b);
              return (
                <div
                  key={b.id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5"
                >
                  <span className="font-medium text-slate-900">#{b.id}</span>
                  <Badge tone={BATCH_STATUS_TONE[b.status]}>{b.status}</Badge>
                  <span className="text-xs tabular-nums text-slate-500">
                    {b.challan_count} challan{b.challan_count === 1 ? '' : 's'} / {b.line_count} line
                    {b.line_count === 1 ? '' : 's'}
                  </span>
                  <div className="ml-auto flex flex-wrap gap-1">
                    {links.length === 0 ? (
                      <span className="text-xs text-slate-400">—</span>
                    ) : (
                      links.map((l) => (
                        <Button
                          key={l.label}
                          variant="ghost"
                          size="sm"
                          onClick={() => onDownload(l.fileId, l.name)}
                        >
                          {l.label}
                        </Button>
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </Card>
        )}
      </div>
    </div>
  );
}
