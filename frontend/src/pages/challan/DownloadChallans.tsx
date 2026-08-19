import { useMemo, useState } from 'react';
import {
  Button,
  Loading,
  PageHeader,
  StatePanel,
  TextArea,
  TextField,
  useToast,
} from '../../ui';
import { useApi } from '../../api/client';
import {
  buildDownloadQuery,
  useDownloadPreview,
  type DownloadMode,
  type DownloadParams,
} from '../../api/challan';
import { errorMessage } from './challanFormat';

/** Join a list of numbers for a human sentence, e.g. "11, 22 and 33". */
function joinNumbers(nums: number[]): string {
  return nums.join(', ');
}

export function DownloadChallans() {
  const toast = useToast();
  const { downloadUrl } = useApi();

  const [series, setSeries] = useState('');
  const [fy, setFy] = useState('');
  const [spec, setSpec] = useState('');
  const [mode, setMode] = useState<DownloadMode>('separate');
  const [downloading, setDownloading] = useState(false);

  // The params committed by the operator's explicit "Preview" click. The preview
  // query (and the Download button) key off THIS, not the live inputs — so what
  // downloads is always exactly what was previewed. Editing any input clears it,
  // forcing a fresh preview before another download (no stale-spec downloads).
  const [submitted, setSubmitted] = useState<DownloadParams | null>(null);

  const canPreview =
    series.trim().length > 0 && fy.trim().length > 0 && spec.trim().length > 0;

  const preview = useDownloadPreview(submitted);
  const data = preview.data;

  // A preview is "valid to download" only when it resolved to at least one
  // challan and reported no spec errors.
  const canDownload =
    submitted != null &&
    !preview.isFetching &&
    data != null &&
    data.errors.length === 0 &&
    data.count > 0;

  // The honest reason the Download button is disabled, surfaced as a hint.
  const disabledReason = useMemo(() => {
    if (submitted == null) return 'Run a preview first.';
    if (preview.isFetching) return 'Previewing…';
    if (data == null) return 'Run a preview first.';
    if (data.errors.length > 0) return 'Fix the errors above, then preview again.';
    if (data.count === 0) return 'No challans matched — nothing to download.';
    return undefined;
  }, [submitted, preview.isFetching, data]);

  /** Clear a stale preview whenever an identifying input changes. */
  function onInputChange<T>(setter: (v: T) => void): (v: T) => void {
    return (v: T) => {
      setter(v);
      if (submitted != null) setSubmitted(null);
    };
  }

  function onPreview() {
    if (!canPreview) return;
    setSubmitted({ series: series.trim(), fy: fy.trim(), spec: spec.trim() });
  }

  async function onDownload() {
    if (!submitted || !canDownload) return;
    setDownloading(true);
    try {
      const ext = mode === 'merged' ? 'pdf' : 'zip';
      const safe = `${submitted.series}-${submitted.fy}`.replace(/\//g, '-');
      await downloadUrl(
        `/challan/download${buildDownloadQuery(submitted, mode)}`,
        `challans-${safe}.${ext}`,
      );
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Download challans"
        subtitle="Pick a series, financial year, and a set of challan numbers to download their PDFs."
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Series"
          value={series}
          onChange={(e) => onInputChange(setSeries)(e.target.value)}
          placeholder="e.g. L"
          maxLength={8}
        />
        <TextField
          label="Financial year"
          value={fy}
          onChange={(e) => onInputChange(setFy)(e.target.value)}
          placeholder="e.g. 26-27"
          maxLength={7}
        />
        <div className="sm:col-span-2 lg:col-span-2">
          <TextArea
            label="Challan numbers"
            value={spec}
            onChange={(e) => onInputChange(setSpec)(e.target.value)}
            placeholder="e.g. 10-50, 55, 60"
            hint="A range and/or a comma-separated list of numbers."
            rows={2}
          />
        </div>
      </div>

      <div className="mb-4">
        <Button onClick={onPreview} disabled={!canPreview} loading={preview.isFetching}>
          Preview
        </Button>
      </div>

      {submitted != null && (
        <div className="mb-5">
          {preview.isFetching ? (
            <Loading label="Previewing…" />
          ) : preview.isError ? (
            <StatePanel tone="red" title="Could not preview this selection">
              {errorMessage(preview.error)}
            </StatePanel>
          ) : data != null ? (
            <div className="space-y-3">
              {/* Spec errors — inline validation, not an error state. */}
              {data.errors.length > 0 && (
                <div
                  role="alert"
                  className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
                >
                  <ul className="list-inside list-disc space-y-0.5">
                    {data.errors.map((msg, i) => (
                      <li key={i}>{msg}</li>
                    ))}
                  </ul>
                </div>
              )}

              {data.errors.length === 0 && (
                <p className="text-sm text-slate-700">
                  <span className="font-semibold text-slate-900">{data.count}</span>{' '}
                  challan{data.count === 1 ? '' : 's'} will download.
                </p>
              )}

              {data.skipped_void.length > 0 && (
                <div
                  role="status"
                  className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
                >
                  Skipping voided challan{data.skipped_void.length === 1 ? '' : 's'}:{' '}
                  <span className="font-medium">{joinNumbers(data.skipped_void)}</span>. Voided
                  challans are never included in a download.
                </div>
              )}

              {data.missing.length > 0 && (
                <div
                  role="status"
                  className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600"
                >
                  Not found in {data.series} / {data.fy}:{' '}
                  <span className="font-medium">{joinNumbers(data.missing)}</span>.
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}

      <fieldset className="mb-4">
        <legend className="mb-2 text-xs font-medium text-slate-600">Format</legend>
        <div className="space-y-2">
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name="download-mode"
              value="separate"
              checked={mode === 'separate'}
              onChange={() => setMode('separate')}
              className="mt-0.5"
            />
            <span className="text-sm">
              <span className="font-medium text-slate-900">Separate PDFs (ZIP)</span>
              <span className="block text-xs text-slate-500">
                One full-A4 PDF per challan, packaged into a single ZIP.
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name="download-mode"
              value="merged"
              checked={mode === 'merged'}
              onChange={() => setMode('merged')}
              className="mt-0.5"
            />
            <span className="text-sm">
              <span className="font-medium text-slate-900">Merged — 2 per A4 page (saves paper)</span>
              <span className="block text-xs text-slate-500">
                A single PDF with two challans on each A4 sheet, to save paper when printing.
              </span>
            </span>
          </label>
        </div>
      </fieldset>

      <div className="flex items-center gap-3">
        <Button onClick={() => void onDownload()} disabled={!canDownload} loading={downloading}>
          Download
        </Button>
        {!canDownload && disabledReason && (
          <span className="text-xs text-slate-400">{disabledReason}</span>
        )}
      </div>
    </div>
  );
}
