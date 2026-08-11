import { useQuery, useMutation, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed client + react-query hooks for the master-data admin screens.
 * Mirrors `backend/app/modules/masterdata/routes.py` exactly — four entities,
 * each soft-disabled (never hard-deleted) via a `/{id}/active` route.
 */

export type MasterKind = 'consignor' | 'consignee' | 'hsn' | 'series';

// ------------------------------------------------------------------ row types

export interface Consignor {
  id: number;
  name: string;
  gstin: string;
  state: string;
  address: string;
  phone: string;
  active: boolean;
}
export interface ConsignorInput {
  name: string;
  gstin: string;
  state: string;
  address: string;
  phone: string;
}

export interface Consignee {
  id: number;
  brand: string;
  state: string;
  name: string;
  gstin: string;
  address: string;
  phone: string;
  active: boolean;
}
export interface ConsigneeInput {
  brand: string;
  state: string;
  name: string;
  gstin: string;
  address: string;
  phone: string;
}

export interface Hsn {
  id: number;
  hsn: string;
  description: string;
  /** Decimal STRING from the backend, e.g. "5.00". */
  gst_rate: string;
  active: boolean;
}
export interface HsnInput {
  hsn: string;
  description: string;
  gst_rate: string;
}

export interface Series {
  id: number;
  letter: string;
  label: string;
  active: boolean;
}
export interface SeriesInput {
  letter: string;
  label: string;
}

// --------------------------------------------------------------- error parsing

export interface ParsedApiError {
  /** field-name -> message, from a 422 pydantic error array. */
  fieldErrors: Record<string, string>;
  /** a human string for 409/403/404, or a model-level 422 message. */
  formError?: string;
}

/**
 * Turn any thrown error into per-field + form-level messages.
 *
 * A 422 puts a pydantic array in `ApiError.body.detail` (`{loc:[...,field], msg}`)
 * — map each entry to its last non-"body" location segment. A 409/403/404 puts a
 * human string in `ApiError.message`. Never surfaces "[object Object]".
 */
export function parseApiError(err: unknown): ParsedApiError {
  if (!(err instanceof ApiError)) {
    return { fieldErrors: {}, formError: err instanceof Error ? err.message : 'Something went wrong.' };
  }

  const body = err.body;
  if (
    err.status === 422 &&
    body &&
    typeof body === 'object' &&
    'detail' in body &&
    Array.isArray((body as { detail: unknown }).detail)
  ) {
    const detail = (body as { detail: unknown[] }).detail;
    const fieldErrors: Record<string, string> = {};
    let formError: string | undefined;
    for (const item of detail) {
      if (!item || typeof item !== 'object' || !('msg' in item)) continue;
      const msg = String((item as { msg: unknown }).msg);
      const loc = 'loc' in item ? (item as { loc: unknown }).loc : undefined;
      const field = Array.isArray(loc)
        ? [...loc].reverse().find((x): x is string => typeof x === 'string' && x !== 'body')
        : undefined;
      if (field) fieldErrors[field] = msg;
      else formError = msg;
    }
    return { fieldErrors, formError };
  }

  return { fieldErrors: {}, formError: err.message };
}

// ------------------------------------------------------------------ query keys

function toQuery(params: Record<string, string | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') sp.set(k, v);
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

// ------------------------------------------------------------------------ hooks

/** List a master-data entity. `params` (e.g. active/brand/state) join the queryKey. */
export function useMasterList<TRow>(
  kind: MasterKind,
  params: Record<string, string | undefined>,
): UseQueryResult<TRow[], Error> {
  const { get } = useApi();
  return useQuery<TRow[], Error>({
    queryKey: ['masterdata', kind, params],
    queryFn: ({ signal }) => get<TRow[]>(`/masterdata/${kind}${toQuery(params)}`, signal),
  });
}

/** Create a row; invalidates the entity list on success. */
export function useMasterCreate<TInput>(kind: MasterKind) {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<TInput, ApiError, TInput>({
    mutationFn: (body) => post<TInput>(`/masterdata/${kind}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['masterdata', kind] });
    },
  });
}

/** Update a row by id; invalidates the entity list on success. */
export function useMasterUpdate<TInput>(kind: MasterKind) {
  const { put } = useApi();
  const qc = useQueryClient();
  return useMutation<TInput, ApiError, { id: number; body: TInput }>({
    mutationFn: ({ id, body }) => put<TInput>(`/masterdata/${kind}/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['masterdata', kind] });
    },
  });
}

/** Enable/disable (soft delete) a row; invalidates the entity list on success. */
export function useMasterToggle(kind: MasterKind) {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<unknown, ApiError, { id: number; active: boolean }>({
    mutationFn: ({ id, active }) => post(`/masterdata/${kind}/${id}/active`, { active }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['masterdata', kind] });
    },
  });
}
