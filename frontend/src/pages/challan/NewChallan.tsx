import { useEffect, useRef, useState } from 'react';
import {
  Badge,
  Button,
  Card,
  PageHeader,
  Spinner,
  StatePanel,
  TextField,
  useToast,
} from '../../ui';
import { useApi } from '../../api/client';
import {
  useBatchQuery,
  useGenerateBatch,
  useUploadBatch,
  type BatchOut,
} from '../../api/challan';
import { BATCH_STATUS_TONE, errorMessage } from './challanFormat';

const SERIES_RE = /^[A-Za-z0-9]{1,8}$/;

export function NewChallan() {
  const toast = useToast();
  const { download, downloadUrl } = useApi();

  async function onDownloadTemplate(): Promise<void> {
    try {
      await downloadUrl('/challan/template.xlsx', 'challan-upload-template.xlsx');
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

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

  const seriesValid = SERIES_RE.test(series);

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

  const showUploadForm = uploaded == null;

  return (
    <div>
      <PageHeader
        title="New Challan"
        subtitle="Upload the filled Excel workbook, review validation, then generate the challans."
        actions={
          <Button variant="secondary" size="sm" onClick={() => void onDownloadTemplate()}>
            Download template
          </Button>
        }
      />

      {showUploadForm && (
        <Card className="max-w-xl p-5">
          <label htmlFor="challan-file" className="mb-1 block text-xs font-medium text-slate-600">
            Excel workbook
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
            A single .xlsx file matching the template columns. It is validated immediately on upload.
          </p>
          <div className="mt-4">
            <Button onClick={onUpload} disabled={!file} loading={upload.isPending}>
              Upload &amp; validate
            </Button>
          </div>
        </Card>
      )}

      {/* Validation failed — show the report + let the user try again. */}
      {uploaded?.status === 'FAILED_VALIDATION' && (
        <Card className="max-w-xl p-5">
          <div className="mb-2 flex items-center gap-2">
            <Badge tone="red">Validation failed</Badge>
            <span className="text-sm text-slate-500">Batch #{uploaded.id}</span>
          </div>
          <p className="text-sm text-slate-600">
            {uploaded.message ?? 'The workbook has validation errors. Fix them and re-upload.'}
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              variant="secondary"
              onClick={() =>
                onDownload(uploaded.error_report_file_id, `batch-${uploaded.id}-errors.csv`)
              }
              disabled={uploaded.error_report_file_id == null}
            >
              Download error report
            </Button>
            <Button variant="ghost" onClick={reset}>
              Try another file
            </Button>
          </div>
        </Card>
      )}

      {/* Validated, not yet generating — show counts + series + generate. */}
      {uploaded?.status === 'VALIDATED' && activeBatchId == null && (
        <Card className="max-w-xl p-5">
          <div className="mb-3 flex items-center gap-2">
            <Badge tone="blue">Validated</Badge>
            <span className="text-sm text-slate-500">Batch #{uploaded.id}</span>
          </div>
          <dl className="mb-4 grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-slate-500">Challans</dt>
              <dd className="text-lg font-semibold text-slate-900">{uploaded.challan_count}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Line items</dt>
              <dd className="text-lg font-semibold text-slate-900">{uploaded.line_count}</dd>
            </div>
          </dl>
          <div className="max-w-[10rem]">
            <TextField
              label="Series"
              value={series}
              onChange={(e) => setSeries(e.target.value)}
              error={series && !seriesValid ? '1–8 letters/digits' : undefined}
              hint="Number prefix, e.g. L"
              maxLength={8}
            />
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              onClick={onGenerate}
              disabled={!seriesValid}
              loading={generate.isPending}
            >
              Generate {uploaded.challan_count} challan{uploaded.challan_count === 1 ? '' : 's'}
            </Button>
            <Button variant="ghost" onClick={reset} disabled={generate.isPending}>
              Cancel
            </Button>
          </div>
        </Card>
      )}

      {/* Generation phase — poll the batch until terminal. */}
      {activeBatchId != null && (
        <Card className="max-w-xl p-5">
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
                  This is taking longer than expected. You can keep this open, or come back
                  to the Batches tab later — the batch will finish and its artifacts appear there.
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
              <p className="text-sm text-rose-600">
                {batch.message ?? 'Generation failed.'}
              </p>
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
        </Card>
      )}
    </div>
  );
}
