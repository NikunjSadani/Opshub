import { Fragment, useState } from 'react';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  TextField,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import { useAuth } from '../../auth/AuthProvider';
import { useApi } from '../../api/client';
import {
  useBatchesQuery,
  useGenerateBatch,
  useRecoverBatch,
  type BatchOut,
} from '../../api/challan';
import { BATCH_STATUS_LABEL, BATCH_STATUS_TONE, batchArtifacts, errorMessage } from './challanFormat';
import { ReviewPanel } from './ReviewPanel';

// Same series shape as New Challan step 3 — kept in sync so a retry from here
// enforces the identical rule.
const SERIES_RE = /^[A-Za-z0-9]{1,8}$/;

export function Batches() {
  const toast = useToast();
  const { user } = useAuth();
  const { download } = useApi();
  const isAdmin = user?.role === 'ADMIN';
  const query = useBatchesQuery(50);
  // Which NEEDS_REVIEW batch (if any) has its inline review panel expanded, so a
  // batch stuck in review is resolvable here — not only inside the upload session.
  const [reviewingId, setReviewingId] = useState<number | null>(null);
  // Which artifact download is in flight (keyed by file id), so a double-click
  // can't start the same download twice.
  const [downloadingId, setDownloadingId] = useState<number | null>(null);

  // Retrying a FAILED (possibly partially-issued) batch re-runs generation for
  // its un-issued challans without a re-upload — the only safe way to finish it
  // after the live upload session is gone (a re-upload would MINT DUPLICATES).
  const generate = useGenerateBatch();
  const recover = useRecoverBatch();
  const [retryTarget, setRetryTarget] = useState<BatchOut | null>(null);
  const [retrySeries, setRetrySeries] = useState('L');
  const [retryError, setRetryError] = useState<string | undefined>();

  async function onDownload(fileId: number | null, fallback: string) {
    if (fileId == null || downloadingId != null) return;
    setDownloadingId(fileId);
    try {
      await download(fileId, fallback);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloadingId(null);
    }
  }

  // The mutation invalidates ['challan','batches'] on resolve, so the row leaves
  // NEEDS_REVIEW on its own; collapse the panel once it is done.
  function onResolved(_updated: BatchOut) {
    setReviewingId(null);
  }

  function openRetry(b: BatchOut) {
    setRetryTarget(b);
    setRetrySeries('L');
    setRetryError(undefined);
    generate.reset();
  }

  function closeRetry() {
    setRetryTarget(null);
    setRetryError(undefined);
    generate.reset();
  }

  function confirmRetry() {
    if (!retryTarget) return;
    if (!SERIES_RE.test(retrySeries)) {
      setRetryError(retrySeries ? '1–8 letters/digits' : 'Series is required');
      return;
    }
    generate.mutate(
      { batchId: retryTarget.id, series: retrySeries },
      {
        onSuccess: (b) => {
          toast.success(`Generation restarted for batch #${b.id}.`);
          closeRetry();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function onRecover(b: BatchOut) {
    recover.mutate(
      { batchId: b.id },
      {
        onSuccess: (u) => toast.success(`Batch #${u.id} recovered — you can retry generation now.`),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <div>
      <PageHeader
        title="Batches"
        subtitle="Recent upload / generation batches and their artifacts."
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void query.refetch()}
            loading={query.isFetching}
          >
            Refresh
          </Button>
        }
      />

      {query.isPending ? (
        <Loading label="Loading batches…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <StatePanel title="No batches yet">
          Upload a workbook on the New Challan tab to get started.
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>ID</Th>
              <Th>Status</Th>
              <Th className="text-right">Challans</Th>
              <Th className="text-right">Lines</Th>
              <Th>Message</Th>
              <Th>Artifacts</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((b) => {
              const links = batchArtifacts(b);
              const needsReview = b.status === 'NEEDS_REVIEW';
              const isFailed = b.status === 'FAILED';
              const isGenerating = b.status === 'GENERATING';
              const expanded = reviewingId === b.id;
              const hasAction = needsReview || isFailed || isGenerating || links.length > 0;
              return (
                <Fragment key={b.id}>
                  <Tr>
                    <Td className="font-medium text-slate-900">#{b.id}</Td>
                    <Td>
                      <Badge tone={BATCH_STATUS_TONE[b.status]}>{BATCH_STATUS_LABEL[b.status]}</Badge>
                    </Td>
                    <Td className="text-right tabular-nums">{b.challan_count}</Td>
                    <Td className="text-right tabular-nums">{b.line_count}</Td>
                    <Td className="max-w-xs text-slate-500">{b.message ?? '—'}</Td>
                    <Td>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {needsReview && (
                          <Button
                            variant="secondary"
                            size="sm"
                            aria-expanded={expanded}
                            onClick={() => setReviewingId(expanded ? null : b.id)}
                          >
                            {expanded ? 'Close review' : 'Review'}
                          </Button>
                        )}
                        {isFailed && (
                          <Button variant="secondary" size="sm" onClick={() => openRetry(b)}>
                            Retry generation
                          </Button>
                        )}
                        {isGenerating &&
                          (isAdmin ? (
                            <Button
                              variant="secondary"
                              size="sm"
                              loading={recover.isPending && recover.variables?.batchId === b.id}
                              onClick={() => onRecover(b)}
                            >
                              Recover
                            </Button>
                          ) : (
                            <span className="text-xs text-slate-500">
                              Generating… if it stays stuck, an admin can recover it.
                            </span>
                          ))}
                        {links.map((l) => (
                          <Button
                            key={l.label}
                            variant="ghost"
                            size="sm"
                            loading={downloadingId === l.fileId}
                            onClick={() => onDownload(l.fileId, l.name)}
                          >
                            {l.label}
                          </Button>
                        ))}
                        {!hasAction && <span className="text-slate-400">—</span>}
                      </div>
                    </Td>
                  </Tr>
                  {needsReview && expanded && (
                    <tr>
                      <td colSpan={6} className="px-3 pb-4">
                        <ReviewPanel batch={b} onDownload={onDownload} onResolved={onResolved} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </Table>
      )}

      <ConfirmDialog
        open={retryTarget != null}
        title="Retry generation"
        confirmLabel="Retry generation"
        loading={generate.isPending}
        onConfirm={confirmRetry}
        onCancel={closeRetry}
        message={
          <div>
            <p className="mb-3">
              Re-run generation for batch{' '}
              <span className="font-semibold">#{retryTarget?.id}</span>. Already-issued challans
              keep their numbers; only the un-issued ones are generated. Do not re-upload the
              workbook — that would create duplicate challan numbers.
            </p>
            <div className="max-w-[10rem]">
              <TextField
                label="Series"
                value={retrySeries}
                onChange={(e) => {
                  setRetrySeries(e.target.value);
                  if (retryError) setRetryError(undefined);
                }}
                error={retryError}
                hint="Number prefix, e.g. L"
                maxLength={8}
              />
            </div>
          </div>
        }
      />
    </div>
  );
}
