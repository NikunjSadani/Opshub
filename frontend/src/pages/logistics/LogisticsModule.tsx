import { useMemo, useState, type ReactElement } from 'react';
import { Link, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  Td,
  TextArea,
  TextField,
  THead,
  Th,
  Tr,
  useToast,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { ApiError } from '../../api/client';
import {
  DELIVERY_STATUSES,
  DELIVERY_STATUS_LABEL,
  DELIVERY_STATUS_TONE,
  useCreateShipment,
  useShipmentsQuery,
  type DeliveryStatus,
  type ShipmentCreateInput,
  type ShipmentFilters,
} from '../../api/logistics';
import { LOGISTICS_BASE } from './logisticsFormat';
import { ShipmentDetail } from './ShipmentDetail';
import { ShipmentUpload } from './ShipmentUpload';

const BASE = LOGISTICS_BASE;

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

/**
 * Logistics tracker — the module's default surface. Owns its own tracker / new /
 * upload / detail sub-routes (mounted at `/m/logistics/*`). Server-side RBAC is the
 * real gate; the create/upload routes are gated at OPERATE here for honest UX.
 */
export function LogisticsModule() {
  const perms = usePermissions();
  const canOperate = perms.atLeast('logistics', 'OPERATE');
  // Defer the OPERATE route guard until /me resolves so a deep-link isn't bounced
  // before permissions load (mirrors the sales-orders / expense guards).
  const operateGuard = (node: ReactElement) =>
    perms.loading ? (
      <div className="grid place-items-center py-10 text-sm text-slate-400">Loading…</div>
    ) : canOperate ? (
      node
    ) : (
      <Navigate to={BASE} replace />
    );

  return (
    <Routes>
      <Route index element={<Register canOperate={canOperate} />} />
      <Route path="new" element={operateGuard(<ShipmentForm />)} />
      <Route path="upload" element={operateGuard(<ShipmentUpload />)} />
      <Route path=":id" element={<ShipmentDetail />} />
      <Route path="*" element={<Navigate to={BASE} replace />} />
    </Routes>
  );
}

/** The filterable shipment tracker register (VIEW). */
function Register({ canOperate }: { canOperate: boolean }) {
  const [q, setQ] = useState('');
  const [challanNumber, setChallanNumber] = useState('');
  const [partner, setPartner] = useState('');
  const [status, setStatus] = useState<DeliveryStatus | ''>('');

  // Memoise on the primitive filter values so the object identity is stable across
  // renders — a fresh literal each render would make the debounced value churn.
  const filters: ShipmentFilters = useMemo(
    () => ({ q, challan_number: challanNumber, partner, status }),
    [q, challanNumber, partner, status],
  );
  const debouncedFilters = useDebouncedValue(filters);
  const query = useShipmentsQuery(debouncedFilters);
  const rows = query.data ?? [];

  return (
    <div>
      <PageHeader
        title="Logistics tracker"
        subtitle="Every shipment tracked against a challan."
        actions={
          canOperate ? (
            <div className="flex items-center gap-2">
              <Link to={`${BASE}/upload`}>
                <Button variant="secondary" size="sm">
                  Upload .xlsx
                </Button>
              </Link>
              <Link to={`${BASE}/new`}>
                <Button size="sm">New shipment</Button>
              </Link>
            </div>
          ) : undefined
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Challan / tracking / consignee"
          maxLength={80}
        />
        <TextField
          label="Challan number"
          value={challanNumber}
          onChange={(e) => setChallanNumber(e.target.value)}
          placeholder="Challan number"
          maxLength={64}
        />
        <TextField
          label="Delivery partner"
          value={partner}
          onChange={(e) => setPartner(e.target.value)}
          placeholder="Partner"
          maxLength={80}
        />
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as DeliveryStatus | '')}
        >
          <option value="">All statuses</option>
          {DELIVERY_STATUSES.map((s) => (
            <option key={s} value={s}>
              {DELIVERY_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
      </div>

      {query.isPending ? (
        <Loading label="Loading shipments…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No shipments found">No shipments match these filters.</StatePanel>
      ) : (
        <>
          <Table>
            <THead>
              <Tr>
                <Th>Challan #</Th>
                <Th>Status</Th>
                <Th>Partner</Th>
                <Th>Tracking</Th>
                <Th>Consignee</Th>
                <Th>Invoice #</Th>
                <Th>PO #</Th>
                <Th>Dispatched</Th>
                <Th>Delivered</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((s) => (
                <Tr key={s.id}>
                  <Td className="font-medium text-slate-900">{s.challan_number}</Td>
                  <Td>
                    <Badge tone={DELIVERY_STATUS_TONE[s.status]}>
                      {DELIVERY_STATUS_LABEL[s.status]}
                    </Badge>
                  </Td>
                  <Td>{s.delivery_partner ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{s.tracking_id ?? '—'}</Td>
                  <Td>{s.consignee_name ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{s.challan_invoice_number ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{s.challan_po_number ?? '—'}</Td>
                  <Td className="whitespace-nowrap">{formatDate(s.dispatched_on)}</Td>
                  <Td className="whitespace-nowrap">{formatDate(s.delivered_on)}</Td>
                  <Td>
                    <div className="flex justify-end">
                      <Link
                        to={`${BASE}/${s.id}`}
                        className="text-sm font-medium text-brand-600 hover:text-brand-700"
                      >
                        View
                      </Link>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          <p className="mt-4 text-sm text-slate-500">Showing {rows.length}</p>
        </>
      )}
    </div>
  );
}

/** Manually create a single shipment (OPERATE). Only the challan number is required. */
function ShipmentForm() {
  const toast = useToast();
  const navigate = useNavigate();
  const create = useCreateShipment();

  const [challanNumber, setChallanNumber] = useState('');
  const [trackingId, setTrackingId] = useState('');
  const [partner, setPartner] = useState('');
  const [status, setStatus] = useState<DeliveryStatus>('PENDING');
  const [consignee, setConsignee] = useState('');
  const [phone, setPhone] = useState('');
  const [pincode, setPincode] = useState('');
  const [address, setAddress] = useState('');
  const [dispatchedOn, setDispatchedOn] = useState('');
  const [deliveredOn, setDeliveredOn] = useState('');
  const [notes, setNotes] = useState('');

  const canSubmit = challanNumber.trim() !== '';

  function onSubmit() {
    if (!canSubmit) return;
    const body: ShipmentCreateInput = {
      challan_number: challanNumber.trim(),
      status,
      tracking_id: trackingId.trim() || undefined,
      delivery_partner: partner.trim() || undefined,
      consignee_name: consignee.trim() || undefined,
      phone: phone.trim() || undefined,
      pincode: pincode.trim() || undefined,
      address: address.trim() || undefined,
      dispatched_on: dispatchedOn || undefined,
      delivered_on: deliveredOn || undefined,
      notes: notes.trim() || undefined,
    };
    create.mutate(body, {
      onSuccess: (shipment) => {
        toast.success('Shipment created.');
        navigate(`${BASE}/${shipment.id}`);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <div>
      <PageHeader
        title="New shipment"
        subtitle="Manually track a delivery against a challan number."
        actions={
          <Link to={BASE} className="text-sm font-medium text-slate-500 hover:text-slate-700">
            Back to tracker
          </Link>
        }
      />

      <div className="max-w-2xl space-y-3">
        <TextField
          label="Challan number"
          required
          value={challanNumber}
          onChange={(e) => setChallanNumber(e.target.value)}
          maxLength={64}
          placeholder="e.g. GIF/DC/26-27/L/000189"
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextField
            label="Tracking ID"
            value={trackingId}
            onChange={(e) => setTrackingId(e.target.value)}
            maxLength={120}
          />
          <TextField
            label="Delivery partner"
            value={partner}
            onChange={(e) => setPartner(e.target.value)}
            maxLength={120}
          />
          <SelectField
            label="Status"
            value={status}
            onChange={(e) => setStatus(e.target.value as DeliveryStatus)}
          >
            {DELIVERY_STATUSES.map((s) => (
              <option key={s} value={s}>
                {DELIVERY_STATUS_LABEL[s]}
              </option>
            ))}
          </SelectField>
          <TextField
            label="Consignee name"
            value={consignee}
            onChange={(e) => setConsignee(e.target.value)}
            maxLength={200}
          />
          <TextField
            label="Dispatched on"
            type="date"
            value={dispatchedOn}
            onChange={(e) => setDispatchedOn(e.target.value)}
          />
          <TextField
            label="Delivered on"
            type="date"
            value={deliveredOn}
            onChange={(e) => setDeliveredOn(e.target.value)}
            min={dispatchedOn || undefined}
          />
          <TextField
            label="Phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            maxLength={40}
          />
          <TextField
            label="Pincode"
            value={pincode}
            onChange={(e) => setPincode(e.target.value)}
            maxLength={10}
          />
        </div>
        <TextArea
          label="Address"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          rows={2}
          maxLength={600}
        />
        <TextArea
          label="Notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          maxLength={1000}
        />

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button onClick={onSubmit} disabled={!canSubmit} loading={create.isPending}>
            Create shipment
          </Button>
          <Link to={BASE}>
            <Button variant="ghost" disabled={create.isPending}>
              Cancel
            </Button>
          </Link>
          {!canSubmit && !create.isPending && (
            <span className="text-xs text-slate-500">A challan number is required.</span>
          )}
        </div>
      </div>
    </div>
  );
}
