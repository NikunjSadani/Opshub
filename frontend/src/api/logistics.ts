import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Logistics module (module key
 * `logistics`). Mirrors the backend app/modules/logistics/routes.py. All paths are
 * relative to `/api/v1` (added by `useApi()`).
 *
 * A shipment tracks one delivery keyed on a challan number; the `challan_*` fields
 * are RESOLVED from the linked challan (may be null when the number can't yet be
 * resolved — a partner dump is never dropped).
 *
 * Ids are treated as strings (mirroring api/projects.ts + api/purchaseOrders.ts):
 * they only ever flow through `<select>` values, query strings, and equality
 * checks — never arithmetic. FastAPI coerces the numeric-string ids back to ints.
 */

// --- status union -------------------------------------------------------------

/** A shipment's delivery lifecycle (backend DeliveryStatus). */
export type DeliveryStatus =
  | 'PENDING'
  | 'DISPATCHED'
  | 'IN_TRANSIT'
  | 'DELIVERED'
  | 'RETURNED'
  | 'FAILED';

/** All statuses, in display order (used by the filter + the status control). */
export const DELIVERY_STATUSES: readonly DeliveryStatus[] = [
  'PENDING',
  'DISPATCHED',
  'IN_TRANSIT',
  'DELIVERED',
  'RETURNED',
  'FAILED',
];

type Tone = 'green' | 'red' | 'amber' | 'slate' | 'blue';

/** Sentence-case labels for a shipment's delivery status. */
export const DELIVERY_STATUS_LABEL: Record<DeliveryStatus, string> = {
  PENDING: 'Pending',
  DISPATCHED: 'Dispatched',
  IN_TRANSIT: 'In transit',
  DELIVERED: 'Delivered',
  RETURNED: 'Returned',
  FAILED: 'Failed',
};

/** Badge tone per status — delivered green, on-the-way amber, bad-outcome red, new slate. */
export const DELIVERY_STATUS_TONE: Record<DeliveryStatus, Tone> = {
  PENDING: 'slate',
  DISPATCHED: 'amber',
  IN_TRANSIT: 'amber',
  DELIVERED: 'green',
  RETURNED: 'red',
  FAILED: 'red',
};

// --- DTOs ---------------------------------------------------------------------

/** A register row (backend shipment summary). The `challan_*` fields are resolved
 * from the linked challan and may be null. */
export interface ShipmentSummary {
  id: string;
  challan_number: string;
  status: DeliveryStatus;
  delivery_partner: string | null;
  tracking_id: string | null;
  consignee_name: string | null;
  /** ISO date string (YYYY-MM-DD) or null. */
  dispatched_on: string | null;
  delivered_on: string | null;
  /** Resolved from the linked challan; null when unresolved. */
  challan_invoice_number: string | null;
  challan_po_number: string | null;
  challan_project_code: string | null;
}

/** A stored POD file reference (backend `pod_file`). */
export interface PodFile {
  id: string;
  filename: string;
}

/** Full shipment detail (summary + the delivery/consignee fields + POD file). */
export interface ShipmentDetail extends ShipmentSummary {
  address: string | null;
  phone: string | null;
  pincode: string | null;
  notes: string | null;
  pod_file: PodFile | null;
  /** ISO datetime strings, when present. */
  created_at?: string | null;
  updated_at?: string | null;
}

/** Manual-create body. Only `challan_number` is required. */
export interface ShipmentCreateInput {
  challan_number: string;
  tracking_id?: string;
  delivery_partner?: string;
  status?: DeliveryStatus;
  consignee_name?: string;
  address?: string;
  phone?: string;
  pincode?: string;
  /** YYYY-MM-DD. */
  dispatched_on?: string | null;
  delivered_on?: string | null;
  notes?: string;
}

/** Update body — only supplied keys apply (status / tracking / partner / dates / etc.). */
export interface ShipmentPatch {
  status?: DeliveryStatus;
  tracking_id?: string | null;
  delivery_partner?: string | null;
  consignee_name?: string | null;
  address?: string | null;
  phone?: string | null;
  pincode?: string | null;
  dispatched_on?: string | null;
  delivered_on?: string | null;
  notes?: string | null;
}

/** One row-level error from a bulk .xlsx upload (unresolved / malformed row). */
export interface ShipmentUploadError {
  row?: number;
  challan_number?: string;
  reason: string;
}

/** The result of a bulk .xlsx upload (upsert on challan_number). */
export interface ShipmentUploadResult {
  created: number;
  updated: number;
  errors: ShipmentUploadError[];
}

export interface ShipmentFilters {
  status?: DeliveryStatus | '';
  challan_number?: string;
  partner?: string;
  /** Free-text search (challan number / tracking / consignee). */
  q?: string;
}

// --- query-string builder (pure, unit-testable) -------------------------------

/**
 * Build a `?status=&challan_number=&partner=&q=` query for the register, appending
 * only non-empty (trimmed) params.
 */
export function buildShipmentsQuery(filters: ShipmentFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set('status', filters.status);
  if (filters.challan_number?.trim()) params.set('challan_number', filters.challan_number.trim());
  if (filters.partner?.trim()) params.set('partner', filters.partner.trim());
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --- query keys ---------------------------------------------------------------

export const logisticsKeys = {
  list: (filters: ShipmentFilters) => ['logistics', 'list', filters] as const,
  // Normalise the id to a string so the detail QUERY (URL param = string) and every
  // mutation's setQueryData/invalidation (backend id = runtime number) land on the
  // SAME cache key — otherwise the open detail never reflects an update/POD attach.
  detail: (id: string | number) => ['logistics', 'detail', String(id)] as const,
};

// --- queries ------------------------------------------------------------------

/** The shipment tracker register, filtered by status / challan# / partner / free-text (VIEW). */
export function useShipmentsQuery(
  filters: ShipmentFilters,
): UseQueryResult<ShipmentSummary[], Error> {
  const { get } = useApi();
  return useQuery<ShipmentSummary[], Error>({
    queryKey: logisticsKeys.list(filters),
    queryFn: ({ signal }) =>
      get<ShipmentSummary[]>(`/logistics/shipments${buildShipmentsQuery(filters)}`, signal),
  });
}

/** A single shipment's full detail (VIEW). */
export function useShipmentQuery(id: string | null): UseQueryResult<ShipmentDetail, Error> {
  const { get } = useApi();
  return useQuery<ShipmentDetail, Error>({
    queryKey: logisticsKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<ShipmentDetail>(`/logistics/shipments/${id}`, signal),
  });
}

// --- mutations ----------------------------------------------------------------

/** Manually create a shipment (shipment.upload = OPERATE). Invalidates the register. */
export function useCreateShipment(): UseMutationResult<
  ShipmentDetail,
  ApiError,
  ShipmentCreateInput
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<ShipmentDetail, ApiError, ShipmentCreateInput>({
    mutationFn: (body) => post<ShipmentDetail>('/logistics/shipments', body),
    onSuccess: (shipment) => {
      qc.setQueryData(logisticsKeys.detail(shipment.id), shipment);
      void qc.invalidateQueries({ queryKey: ['logistics', 'list'] });
    },
  });
}

/**
 * Bulk-upload shipments from an .xlsx (shipment.upload = OPERATE). Sends a multipart
 * form with `file`; the backend upserts on challan_number. Returns the
 * created / updated / errored breakdown. Invalidates the register on success.
 */
export function useUploadShipments(): UseMutationResult<ShipmentUploadResult, ApiError, File> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<ShipmentUploadResult, ApiError, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append('file', file);
      return postForm<ShipmentUploadResult>('/logistics/shipments/upload', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['logistics', 'list'] });
    },
  });
}

/** Update a shipment's status / tracking / partner / dates / etc. (OPERATE). */
export function useUpdateShipment(): UseMutationResult<
  ShipmentDetail,
  ApiError,
  { id: string; body: ShipmentPatch }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<ShipmentDetail, ApiError, { id: string; body: ShipmentPatch }>({
    mutationFn: ({ id, body }) => patch<ShipmentDetail>(`/logistics/shipments/${id}`, body),
    onSuccess: (shipment) => {
      qc.setQueryData(logisticsKeys.detail(shipment.id), shipment);
      void qc.invalidateQueries({ queryKey: ['logistics', 'list'] });
    },
  });
}

/**
 * Attach a POD to a shipment (OPERATE). Two-step: upload the file to `/files/upload`
 * (tagged `module_key=logistics`), then link the returned file id via
 * `POST /logistics/shipments/{id}/pod`. Returns the updated shipment.
 */
export function useSetPod(): UseMutationResult<
  ShipmentDetail,
  ApiError,
  { id: string; file: File }
> {
  const { post, postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<ShipmentDetail, ApiError, { id: string; file: File }>({
    mutationFn: async ({ id, file }) => {
      const form = new FormData();
      form.append('file', file);
      form.append('module_key', 'logistics');
      const uploaded = await postForm<{ id: number | string; filename: string }>(
        '/files/upload',
        form,
      );
      return post<ShipmentDetail>(`/logistics/shipments/${id}/pod`, {
        pod_file_id: String(uploaded.id),
      });
    },
    onSuccess: (shipment) => {
      qc.setQueryData(logisticsKeys.detail(shipment.id), shipment);
      void qc.invalidateQueries({ queryKey: ['logistics', 'list'] });
    },
  });
}

/** Delete a shipment (shipment.manage = MANAGE). Invalidates the register. */
export function useDeleteShipment(): UseMutationResult<void, ApiError, string> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => del<void>(`/logistics/shipments/${id}`),
    onSuccess: (_data, id) => {
      qc.removeQueries({ queryKey: logisticsKeys.detail(id) });
      void qc.invalidateQueries({ queryKey: ['logistics', 'list'] });
    },
  });
}
