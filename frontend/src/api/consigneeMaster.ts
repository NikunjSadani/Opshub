import { useQuery, useMutation, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed client + react-query hooks for the GSTIN-keyed Consignee Master.
 *
 * This is the new bill-to-party repository (`backend .../masterdata/consignee-parties`),
 * distinct from the legacy brand→state "Consignee" entity in `masterdata.ts`. The
 * GSTIN is the natural key: settable on create, immutable thereafter (PATCH omits it).
 */

export type ConsigneePartySource = 'MANUAL' | 'UPLOAD';

// ------------------------------------------------------------------ row + inputs

export interface ConsigneeParty {
  id: number;
  gstin: string;
  name: string;
  address_line1: string;
  address_line2: string;
  pincode: string;
  state: string;
  phone: string;
  source: ConsigneePartySource;
  /** ISO timestamp of the last write. */
  updated_at: string;
}

/** Create body — gstin is required; the backend runs the real checksum (422 on fail). */
export interface ConsigneePartyCreate {
  gstin: string;
  name: string;
  address_line1?: string;
  address_line2?: string;
  pincode?: string;
  state?: string;
  phone?: string;
}

/** Update body — every editable field EXCEPT the (immutable) gstin. */
export type ConsigneePartyUpdate = Omit<ConsigneePartyCreate, 'gstin'>;

// ------------------------------------------------------------------------ hooks

const KEY = 'consignee-parties';

/**
 * List consignee parties, optionally filtered by a free-text `q`. The trimmed `q`
 * joins the queryKey so each distinct search is cached independently.
 */
export function useConsigneePartiesQuery(q?: string): UseQueryResult<ConsigneeParty[], Error> {
  const { get } = useApi();
  const term = q?.trim() ?? '';
  const suffix = term ? `?q=${encodeURIComponent(term)}` : '';
  return useQuery<ConsigneeParty[], Error>({
    queryKey: [KEY, term],
    queryFn: ({ signal }) => get<ConsigneeParty[]>(`/masterdata/consignee-parties${suffix}`, signal),
  });
}

/** Create a consignee party; invalidates every cached list on success. */
export function useCreateConsigneeParty() {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<ConsigneeParty, ApiError, ConsigneePartyCreate>({
    mutationFn: (body) => post<ConsigneeParty>('/masterdata/consignee-parties', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}

/** Update a consignee party by id (gstin cannot change); invalidates the lists. */
export function useUpdateConsigneeParty() {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<ConsigneeParty, ApiError, { id: number; body: ConsigneePartyUpdate }>({
    mutationFn: ({ id, body }) => patch<ConsigneeParty>(`/masterdata/consignee-parties/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [KEY] });
    },
  });
}
