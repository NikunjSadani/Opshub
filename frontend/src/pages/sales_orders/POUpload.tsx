import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  PageHeader,
  SelectField,
  StatePanel,
  useToast,
} from '../../ui';
import { ApiError } from '../../api/client';
import { useClientsQuery, useProjectsQuery } from '../../api/projects';
import { useBulkUploadPOs, type BulkUploadOut } from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** Bulk-upload POs from an .xlsx, tagged with one client + project (OPERATE). */
export function POUpload() {
  const toast = useToast();
  const clientsQuery = useClientsQuery();
  const upload = useBulkUploadPOs();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [clientId, setClientId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<BulkUploadOut | null>(null);

  // Projects are scoped to the chosen client + ACTIVE (the backend requires both).
  const projectsQuery = useProjectsQuery({ client_id: clientId, status: 'ACTIVE' });
  const projects = projectsQuery.data ?? [];

  function onClientChange(id: string) {
    setClientId(id);
    setProjectId('');
  }

  function reset() {
    setFile(null);
    setResult(null);
    upload.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  const canSubmit = !!clientId && !!projectId && file != null;

  function onUpload() {
    if (!canSubmit || !file) return;
    setResult(null);
    upload.mutate(
      { file, clientId, projectId },
      {
        onSuccess: (out) => {
          setResult(out);
          const errs = out.errors.length;
          const skips = out.skipped.length;
          if (errs > 0 || skips > 0) {
            toast.info(
              `Created ${out.created.length}. ${skips} skipped, ${errs} row error${
                errs === 1 ? '' : 's'
              } — see the summary below.`,
            );
          } else {
            toast.success(
              `Created ${out.created.length} purchase order${out.created.length === 1 ? '' : 's'}.`,
            );
          }
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  if (clientsQuery.isError) {
    return <ErrorState error={clientsQuery.error} onRetry={() => void clientsQuery.refetch()} />;
  }

  return (
    <div>
      <PageHeader
        title="Bulk upload purchase orders"
        subtitle="Upload one .xlsx with many POs. Rows are grouped by po_number; each PO is tagged with the client + project chosen here."
        actions={
          <Link
            to={SALES_ORDERS_BASE}
            className="text-sm font-medium text-slate-500 hover:text-slate-700"
          >
            Back to register
          </Link>
        }
      />

      <div className="max-w-2xl">
        <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <SelectField
            label="Client"
            required
            value={clientId}
            onChange={(e) => onClientChange(e.target.value)}
            disabled={clientsQuery.isPending}
          >
            <option value="">
              {clientsQuery.isPending ? 'Loading clients…' : 'Select a client…'}
            </option>
            {(clientsQuery.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.code} — {c.name}
              </option>
            ))}
          </SelectField>
          <div>
            <SelectField
              label="Project"
              required
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              disabled={!clientId || projectsQuery.isPending || projectsQuery.isError}
              error={
                clientId && projectsQuery.isError ? "Couldn't load projects." : undefined
              }
            >
              <option value="">
                {!clientId
                  ? 'Select a client first…'
                  : projectsQuery.isPending
                    ? 'Loading projects…'
                    : projectsQuery.isError
                      ? 'Failed to load projects'
                      : projects.length === 0
                        ? 'No active projects for this client'
                        : 'Select a project…'}
              </option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.code} — {p.name}
                </option>
              ))}
            </SelectField>
            {clientId && projectsQuery.isError && (
              <button
                type="button"
                onClick={() => void projectsQuery.refetch()}
                className="mt-1 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                Retry
              </button>
            )}
          </div>
        </div>

        <label htmlFor="po-file" className="mb-1 block text-xs font-medium text-slate-600">
          Excel file (.xlsx)
        </label>
        <input
          id="po-file"
          ref={fileInputRef}
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          Columns: po_number, product_code (or product_name), description, uom, ordered_qty,
          cost_price, sell_price, freight, packaging, handling, other, tax_rate. Money columns are
          in rupees.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button onClick={onUpload} disabled={!canSubmit} loading={upload.isPending}>
            Upload
          </Button>
          {(result != null || file != null) && (
            <Button variant="ghost" onClick={reset} disabled={upload.isPending}>
              Clear
            </Button>
          )}
          {!canSubmit && !upload.isPending && (
            <span className="text-xs text-slate-500">
              Choose a client, project, and an .xlsx file to enable upload.
            </span>
          )}
        </div>
      </div>

      {result != null && (
        <div className="mt-8 max-w-2xl space-y-5">
          <h2 className="text-sm font-semibold text-slate-900">Upload summary</h2>

          <div className="flex flex-wrap gap-4 text-sm">
            <span className="text-emerald-700">Created: {result.created.length}</span>
            <span className="text-amber-700">Skipped: {result.skipped.length}</span>
            <span className="text-rose-700">Row errors: {result.errors.length}</span>
          </div>

          {result.created.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Created
              </h3>
              <div className="flex flex-wrap gap-2">
                {result.created.map((po) => (
                  <Badge key={po} tone="green">
                    {po}
                  </Badge>
                ))}
              </div>
            </section>
          )}

          {/* Skipped + errored rows are ALWAYS shown in full — never collapsed or hidden. */}
          {result.skipped.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-amber-700">
                Skipped
              </h3>
              <ul className="divide-y divide-slate-100 rounded-xl border border-amber-200 bg-amber-50/40">
                {result.skipped.map((s, i) => (
                  <li key={`${s.po_number}-${i}`} className="px-3 py-2 text-sm">
                    <span className="font-medium text-slate-900">{s.po_number}</span>
                    <span className="text-slate-600"> — {s.reason}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {result.errors.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-rose-700">
                Row errors
              </h3>
              <ul className="divide-y divide-slate-100 rounded-xl border border-rose-200 bg-rose-50/40">
                {result.errors.map((e, i) => (
                  <li key={`${e.row}-${i}`} className="px-3 py-2 text-sm">
                    <span className="font-medium text-slate-900">Row {e.row}</span>
                    <span className="text-slate-600"> — {e.reason}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {result.created.length === 0 &&
            result.skipped.length === 0 &&
            result.errors.length === 0 && (
              <StatePanel title="Nothing in this file">
                No PO rows were found in the uploaded spreadsheet.
              </StatePanel>
            )}
        </div>
      )}
    </div>
  );
}
