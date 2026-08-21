import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button, PageHeader, StatePanel, useToast } from '../../ui';
import { ApiError } from '../../api/client';
import { useUploadShipments, type ShipmentUploadResult } from '../../api/logistics';
import { LOGISTICS_BASE } from './logisticsFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** Bulk-upload shipments from a delivery-partner .xlsx dump (OPERATE). */
export function ShipmentUpload() {
  const toast = useToast();
  const upload = useUploadShipments();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<ShipmentUploadResult | null>(null);

  function reset() {
    setFile(null);
    setResult(null);
    upload.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function onUpload() {
    if (!file) return;
    setResult(null);
    upload.mutate(file, {
      onSuccess: (out) => {
        setResult(out);
        const errs = out.errors.length;
        if (errs > 0) {
          toast.info(
            `Created ${out.created}, updated ${out.updated}. ${errs} row error${
              errs === 1 ? '' : 's'
            } — see the summary below.`,
          );
        } else {
          toast.success(`Created ${out.created}, updated ${out.updated}.`);
        }
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <div>
      <PageHeader
        title="Upload shipments"
        subtitle="Upload one .xlsx delivery-partner dump. Rows upsert on challan number — an existing shipment is updated, a new one is created."
        actions={
          <Link
            to={LOGISTICS_BASE}
            className="text-sm font-medium text-slate-500 hover:text-slate-700"
          >
            Back to tracker
          </Link>
        }
      />

      <div className="max-w-2xl">
        <label htmlFor="logistics-file" className="mb-1 block text-xs font-medium text-slate-600">
          Excel file (.xlsx)
        </label>
        <input
          id="logistics-file"
          ref={fileInputRef}
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          Columns: challan_number, tracking_id, delivery_partner, status, consignee_name, address,
          phone, pincode, dispatched_on, delivered_on, notes. Rows key on challan_number.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button onClick={onUpload} disabled={file == null} loading={upload.isPending}>
            Upload
          </Button>
          {(result != null || file != null) && (
            <Button variant="ghost" onClick={reset} disabled={upload.isPending}>
              Clear
            </Button>
          )}
          {file == null && !upload.isPending && (
            <span className="text-xs text-slate-500">Choose an .xlsx file to enable upload.</span>
          )}
        </div>
      </div>

      {result != null && (
        <div className="mt-8 max-w-2xl space-y-5">
          <h2 className="text-sm font-semibold text-slate-900">Upload summary</h2>

          <div className="flex flex-wrap gap-4 text-sm">
            <span className="text-emerald-700">Created: {result.created}</span>
            <span className="text-brand-700">Updated: {result.updated}</span>
            <span className="text-rose-700">Row errors: {result.errors.length}</span>
          </div>

          {/* Errored rows are ALWAYS shown in full — never collapsed or hidden. */}
          {result.errors.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-rose-700">
                Row errors
              </h3>
              <ul className="divide-y divide-slate-100 rounded-xl border border-rose-200 bg-rose-50/40">
                {result.errors.map((e, i) => (
                  <li key={i} className="px-3 py-2 text-sm">
                    <span className="font-medium text-slate-900">
                      {e.row != null
                        ? `Row ${e.row}`
                        : e.challan_number
                          ? e.challan_number
                          : 'Row'}
                    </span>
                    <span className="text-slate-600"> — {e.message}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {result.created === 0 && result.updated === 0 && result.errors.length === 0 && (
            <StatePanel title="Nothing in this file">
              No shipment rows were found in the uploaded spreadsheet.
            </StatePanel>
          )}
        </div>
      )}
    </div>
  );
}
