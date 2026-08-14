import { Fragment, useState } from 'react';
import {
  Badge,
  Button,
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
import { useApi } from '../../api/client';
import { useBatchesQuery, type BatchOut } from '../../api/challan';
import { BATCH_STATUS_LABEL, BATCH_STATUS_TONE, batchArtifacts, errorMessage } from './challanFormat';
import { ReviewPanel } from './ReviewPanel';

export function Batches() {
  const toast = useToast();
  const { download } = useApi();
  const query = useBatchesQuery(50);
  // Which NEEDS_REVIEW batch (if any) has its inline review panel expanded, so a
  // batch stuck in review is resolvable here — not only inside the upload session.
  const [reviewingId, setReviewingId] = useState<number | null>(null);

  async function onDownload(fileId: number | null, fallback: string) {
    if (fileId == null) return;
    try {
      await download(fileId, fallback);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  // The mutation invalidates ['challan','batches'] on resolve, so the row leaves
  // NEEDS_REVIEW on its own; collapse the panel once it is done.
  function onResolved(_updated: BatchOut) {
    setReviewingId(null);
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
              const expanded = reviewingId === b.id;
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
                        {links.length === 0 && !needsReview ? (
                          <span className="text-slate-400">—</span>
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
    </div>
  );
}
