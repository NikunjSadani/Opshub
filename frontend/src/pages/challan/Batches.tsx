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
import { BATCH_STATUS_TONE, errorMessage } from './challanFormat';

export function Batches() {
  const toast = useToast();
  const { download } = useApi();
  const query = useBatchesQuery(50);

  async function onDownload(fileId: number | null, fallback: string) {
    if (fileId == null) return;
    try {
      await download(fileId, fallback);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }

  function artifacts(b: BatchOut) {
    const links: Array<{ label: string; fileId: number; name: string }> = [];
    if (b.error_report_file_id != null)
      links.push({ label: 'Error report', fileId: b.error_report_file_id, name: `batch-${b.id}-errors.csv` });
    if (b.zip_file_id != null)
      links.push({ label: 'ZIP', fileId: b.zip_file_id, name: `batch-${b.id}.zip` });
    if (b.merged_pdf_file_id != null)
      links.push({ label: 'Merged PDF', fileId: b.merged_pdf_file_id, name: `batch-${b.id}.pdf` });
    return links;
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
              const links = artifacts(b);
              return (
                <Tr key={b.id}>
                  <Td className="font-medium text-slate-900">#{b.id}</Td>
                  <Td>
                    <Badge tone={BATCH_STATUS_TONE[b.status]}>{b.status}</Badge>
                  </Td>
                  <Td className="text-right tabular-nums">{b.challan_count}</Td>
                  <Td className="text-right tabular-nums">{b.line_count}</Td>
                  <Td className="max-w-xs text-slate-500">{b.message ?? '—'}</Td>
                  <Td>
                    {links.length === 0 ? (
                      <span className="text-slate-400">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1.5">
                        {links.map((l) => (
                          <Button
                            key={l.label}
                            variant="ghost"
                            size="sm"
                            onClick={() => onDownload(l.fileId, l.name)}
                          >
                            {l.label}
                          </Button>
                        ))}
                      </div>
                    )}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </div>
  );
}
