import { useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  StatePanel,
  useToast,
} from '../../ui';
import { ApiError, useApi } from '../../api/client';
import { usePermissions } from '../../auth/AuthProvider';
import {
  DELIVERY_STATUSES,
  DELIVERY_STATUS_LABEL,
  DELIVERY_STATUS_TONE,
  useDeleteShipment,
  useSetPod,
  useShipmentQuery,
  useUpdateShipment,
  type DeliveryStatus,
  type ShipmentDetail as ShipmentDetailType,
} from '../../api/logistics';
import { LOGISTICS_BASE } from './logisticsFormat';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return 'Something went wrong.';
}

/** Format a plain YYYY-MM-DD as DD/MM/YYYY without a timezone-shifting parse. */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

function DefItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-800">{children}</dd>
    </div>
  );
}

export function ShipmentDetail() {
  const { id = '' } = useParams();
  const query = useShipmentQuery(id || null);

  if (query.isPending) return <Loading label="Loading shipment…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.data) return <StatePanel title="Not found">This shipment does not exist.</StatePanel>;

  return <ShipmentDetailBody shipment={query.data} />;
}

function ShipmentDetailBody({ shipment }: { shipment: ShipmentDetailType }) {
  const toast = useToast();
  const navigate = useNavigate();
  const perms = usePermissions();
  const { download } = useApi();

  const canOperate = perms.atLeast('logistics', 'OPERATE');
  const canManage = perms.atLeast('logistics', 'MANAGE');

  const update = useUpdateShipment();
  const setPod = useSetPod();
  const del = useDeleteShipment();
  const podInputRef = useRef<HTMLInputElement>(null);

  // The inline status control is a staged edit: it starts at the current status and
  // only PATCHes when "Update" is pressed, so a mis-select doesn't fire a request.
  const [status, setStatus] = useState<DeliveryStatus>(shipment.status);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [downloadingPod, setDownloadingPod] = useState(false);

  const statusChanged = status !== shipment.status;

  function onUpdateStatus() {
    if (!statusChanged) return;
    update.mutate(
      { id: shipment.id, body: { status } },
      {
        onSuccess: () => toast.success('Status updated.'),
        onError: (err) => {
          toast.error(errorMessage(err));
          // Roll the control back to the server truth on failure.
          setStatus(shipment.status);
        },
      },
    );
  }

  function onPodChange(fileList: FileList | null) {
    const chosen = fileList?.[0];
    if (!chosen) return;
    setPod.mutate(
      { id: shipment.id, file: chosen },
      {
        onSuccess: () => toast.success('Proof of delivery attached.'),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
    if (podInputRef.current) podInputRef.current.value = '';
  }

  function onDelete() {
    del.mutate(shipment.id, {
      onSuccess: () => {
        toast.success('Shipment deleted.');
        setDeleteOpen(false);
        navigate(LOGISTICS_BASE);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <div>
      <PageHeader
        title={`Shipment ${shipment.challan_number}`}
        subtitle={shipment.consignee_name ?? undefined}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to={LOGISTICS_BASE}
              className="text-sm font-medium text-slate-500 hover:text-slate-700"
            >
              Back to tracker
            </Link>
            {canManage && (
              <Button variant="danger" size="sm" onClick={() => setDeleteOpen(true)}>
                Delete
              </Button>
            )}
          </div>
        }
      />

      <div className="mb-6 rounded-xl border border-slate-200 bg-white p-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <DefItem label="Status">
            <Badge tone={DELIVERY_STATUS_TONE[shipment.status]}>
              {DELIVERY_STATUS_LABEL[shipment.status]}
            </Badge>
          </DefItem>
          <DefItem label="Delivery partner">{shipment.delivery_partner ?? '—'}</DefItem>
          <DefItem label="Tracking ID">{shipment.tracking_id ?? '—'}</DefItem>
          <DefItem label="Consignee">{shipment.consignee_name ?? '—'}</DefItem>
          <DefItem label="Dispatched on">{formatDate(shipment.dispatched_on)}</DefItem>
          <DefItem label="Delivered on">{formatDate(shipment.delivered_on)}</DefItem>
          <DefItem label="Invoice #">{shipment.challan_invoice_number ?? '—'}</DefItem>
          <DefItem label="PO #">{shipment.challan_po_number ?? '—'}</DefItem>
          <DefItem label="Project">{shipment.challan_project_code ?? '—'}</DefItem>
          <DefItem label="Phone">{shipment.phone ?? '—'}</DefItem>
          <DefItem label="Pincode">{shipment.pincode ?? '—'}</DefItem>
          <DefItem label="Address">{shipment.address ?? '—'}</DefItem>
        </dl>
        {shipment.notes && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">Notes</dt>
            <dd className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700">{shipment.notes}</dd>
          </div>
        )}
      </div>

      {/* Inline status update — OPERATE. */}
      {canOperate && (
        <div className="mb-6 rounded-xl border border-slate-200 bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-900">Update status</h2>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-48">
              <SelectField
                label="Delivery status"
                value={status}
                onChange={(e) => setStatus(e.target.value as DeliveryStatus)}
              >
                {DELIVERY_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {DELIVERY_STATUS_LABEL[s]}
                  </option>
                ))}
              </SelectField>
            </div>
            <Button
              onClick={onUpdateStatus}
              disabled={!statusChanged}
              loading={update.isPending}
            >
              Update
            </Button>
          </div>
        </div>
      )}

      {/* Proof of delivery — download if present, attach/replace at OPERATE. */}
      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-900">Proof of delivery</h2>
        {shipment.pod_file ? (
          <div className="mb-3 flex items-center gap-3 text-sm">
            <button
              type="button"
              disabled={downloadingPod}
              onClick={() => {
                if (downloadingPod) return;
                setDownloadingPod(true);
                void download(Number(shipment.pod_file!.id), shipment.pod_file!.filename)
                  .catch((err) => toast.error(errorMessage(err)))
                  .finally(() => setDownloadingPod(false));
              }}
              className="font-medium text-brand-600 hover:text-brand-700 disabled:opacity-50"
            >
              {downloadingPod ? 'Downloading…' : shipment.pod_file.filename}
            </button>
          </div>
        ) : (
          <p className="mb-3 text-sm text-slate-500">No proof of delivery attached yet.</p>
        )}

        {canOperate && (
          <div className="flex items-center gap-3">
            <input
              ref={podInputRef}
              type="file"
              disabled={setPod.isPending}
              onChange={(e) => onPodChange(e.target.files)}
              className="block w-full max-w-md text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100 disabled:opacity-50"
            />
            {setPod.isPending && <span className="text-xs text-slate-500">Uploading…</span>}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={deleteOpen}
        title="Delete shipment"
        confirmLabel="Delete shipment"
        danger
        loading={del.isPending}
        onCancel={() => {
          if (!del.isPending) setDeleteOpen(false);
        }}
        onConfirm={onDelete}
        message={
          <p>
            Delete shipment <span className="font-semibold">{shipment.challan_number}</span>? This
            permanently removes the tracking row and cannot be undone.
          </p>
        }
      />
    </div>
  );
}
