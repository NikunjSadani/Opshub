import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge, Button, PageHeader, StatePanel, useToast } from '../../ui';
import { ApiError } from '../../api/client';
import {
  useBulkUploadProducts,
  useDownloadProductsTemplate,
  type ProductsBulkResult,
} from '../../api/products';
import { SALES_ORDERS_BASE } from './salesOrdersFormat';

/** The Products tab's route base — where this upload page's "Back" link returns. */
const PRODUCTS_BASE = `${SALES_ORDERS_BASE}/products`;

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/**
 * Bulk-upload products from an .xlsx (MANAGE). Mirrors POUpload: a Download-template
 * button + file input + Upload, then a full created / updated / errored outcome summary.
 * Row errors are ALWAYS shown in full — never collapsed or hidden.
 */
export function ProductsUpload() {
  const toast = useToast();
  const upload = useBulkUploadProducts();
  const downloadTemplate = useDownloadProductsTemplate();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<ProductsBulkResult | null>(null);
  const [downloading, setDownloading] = useState(false);

  async function onDownloadTemplate() {
    setDownloading(true);
    try {
      await downloadTemplate();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  function reset() {
    setFile(null);
    setResult(null);
    upload.reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  const canSubmit = file != null;

  function onUpload() {
    if (!canSubmit || !file) return;
    setResult(null);
    upload.mutate(file, {
      onSuccess: (out) => {
        setResult(out);
        const errs = out.errors.length;
        const touched = out.created.length + out.updated.length;
        if (errs > 0) {
          toast.info(
            `${touched} product${touched === 1 ? '' : 's'} saved, ${errs} row error${
              errs === 1 ? '' : 's'
            } — see the summary below.`,
          );
        } else {
          toast.success(
            `Saved ${touched} product${touched === 1 ? '' : 's'} (${out.created.length} created, ${
              out.updated.length
            } updated).`,
          );
        }
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <div>
      <PageHeader
        title="Bulk upload products"
        subtitle="Upload one .xlsx to add products in bulk. A new product's code is auto-generated; to update existing products instead, add a code column with their code."
        actions={
          <Link
            to={PRODUCTS_BASE}
            className="text-sm font-medium text-slate-500 hover:text-slate-700"
          >
            Back to products
          </Link>
        }
      />

      <div className="max-w-2xl">
        <label htmlFor="products-file" className="mb-1 block text-xs font-medium text-slate-600">
          Excel file (.xlsx)
        </label>
        <input
          id="products-file"
          ref={fileInputRef}
          type="file"
          accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <p className="mt-1 text-xs text-slate-400">
          Template columns: name, brand, model_number, category, uom, hsn, gst_rate — a new product's
          code is auto-generated. To UPDATE existing products instead, add a{' '}
          <span className="font-medium">code</span> column with their code.
        </p>
        <div className="mt-2">
          <Button variant="ghost" onClick={() => void onDownloadTemplate()} loading={downloading}>
            Download template
          </Button>
        </div>

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
            <span className="text-xs text-slate-500">Choose an .xlsx file to enable upload.</span>
          )}
        </div>
      </div>

      {result != null && (
        <div className="mt-8 max-w-2xl space-y-5">
          <h2 className="text-sm font-semibold text-slate-900">Upload summary</h2>

          <div className="flex flex-wrap gap-4 text-sm">
            <span className="text-emerald-700">Created: {result.created.length}</span>
            <span className="text-blue-700">Updated: {result.updated.length}</span>
            <span className="text-rose-700">Row errors: {result.errors.length}</span>
          </div>

          {result.created.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Created
              </h3>
              <div className="flex flex-wrap gap-2">
                {result.created.map((code) => (
                  <Badge key={code} tone="green">
                    {code}
                  </Badge>
                ))}
              </div>
            </section>
          )}

          {result.updated.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Updated
              </h3>
              <div className="flex flex-wrap gap-2">
                {result.updated.map((code) => (
                  <Badge key={code} tone="blue">
                    {code}
                  </Badge>
                ))}
              </div>
            </section>
          )}

          {/* Row errors are ALWAYS shown in full — never collapsed or hidden. */}
          {result.errors.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-rose-700">
                Row errors
              </h3>
              <ul className="divide-y divide-slate-100 rounded-xl border border-rose-200 bg-rose-50/40">
                {result.errors.map((e, i) => (
                  <li key={`${e.row}-${i}`} className="px-3 py-2 text-sm">
                    <span className="font-medium text-slate-900">Row {e.row}</span>
                    <span className="text-slate-600"> — {e.message}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {result.created.length === 0 &&
            result.updated.length === 0 &&
            result.errors.length === 0 && (
              <StatePanel title="Nothing in this file">
                No product rows were found in the uploaded spreadsheet.
              </StatePanel>
            )}
        </div>
      )}
    </div>
  );
}
