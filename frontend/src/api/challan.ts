import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi } from './client';

/**
 * Typed contracts + React Query hooks for the Delivery Challan module.
 * Mirrors backend/app/modules/challan/routes.py (BatchOut / ChallanOut / GenerateBody
 * / VoidBody). All paths are relative to `/api/v1` (added by `useApi()`).
 */

// --- status unions (backend BatchStatus / ChallanStatus) ---
export type BatchStatus =
  | 'PENDING'
  | 'FAILED_VALIDATION'
  | 'VALIDATED'
  | 'GENERATING'
  | 'COMPLETED'
  | 'FAILED';

export type ChallanStatus = 'ISSUED' | 'VOID';

/** A batch is done rendering (no more polling) when it reaches one of these. */
const TERMINAL_BATCH: readonly BatchStatus[] = [
  'PENDING',
  'FAILED_VALIDATION',
  'VALIDATED',
  'COMPLETED',
  'FAILED',
];

export interface BatchOut {
  id: number;
  status: BatchStatus;
  challan_count: number;
  line_count: number;
  message: string | null;
  error_report_file_id: number | null;
  zip_file_id: number | null;
  merged_pdf_file_id: number | null;
}

export interface ChallanOut {
  id: number;
  number: string;
  series: string;
  fy: string;
  /** ISO date string (backend `date`). */
  challan_date: string;
  consignee_brand: string;
  consignee_name: string;
  ship_to_state: string;
  eway_required: boolean;
  /** PAISE; null = a value-free challan. */
  total_paise: number | null;
  status: ChallanStatus;
  pdf_file_id: number | null;
}

export interface ChallanFilters {
  series?: string;
  fy?: string;
  status?: ChallanStatus | '';
}

// --------------------------------------------------------------- query keys
export const challanKeys = {
  batches: (limit: number) => ['challan', 'batches', limit] as const,
  batch: (id: number) => ['challan', 'batch', id] as const,
  challans: (filters: ChallanFilters) => ['challan', 'challans', filters] as const,
};

// ------------------------------------------------------------------ queries

/** Recent batches (newest first). */
export function useBatchesQuery(limit = 50): UseQueryResult<BatchOut[], Error> {
  const { get } = useApi();
  return useQuery<BatchOut[], Error>({
    queryKey: challanKeys.batches(limit),
    queryFn: ({ signal }) => get<BatchOut[]>(`/challan/batches?limit=${limit}`, signal),
  });
}

/**
 * A single batch. While it is GENERATING the query self-refetches (default
 * every 1.5s) so the caller can show live progress; polling stops the moment
 * the batch reaches a terminal status.
 */
export function useBatchQuery(
  batchId: number | null,
  pollMs = 1500,
): UseQueryResult<BatchOut, Error> {
  const { get } = useApi();
  return useQuery<BatchOut, Error>({
    queryKey: challanKeys.batch(batchId ?? -1),
    enabled: batchId != null,
    queryFn: ({ signal }) => get<BatchOut>(`/challan/batches/${batchId}`, signal),
    refetchInterval: (query) =>
      query.state.data && !TERMINAL_BATCH.includes(query.state.data.status) ? pollMs : false,
  });
}

/** The challan register, filtered by series / fy / status. */
export function useChallansQuery(filters: ChallanFilters): UseQueryResult<ChallanOut[], Error> {
  const { get } = useApi();
  return useQuery<ChallanOut[], Error>({
    queryKey: challanKeys.challans(filters),
    queryFn: ({ signal }) => {
      const params = new URLSearchParams();
      if (filters.series?.trim()) params.set('series', filters.series.trim());
      if (filters.fy?.trim()) params.set('fy', filters.fy.trim());
      if (filters.status) params.set('status', filters.status);
      const qs = params.toString();
      return get<ChallanOut[]>(`/challan/challans${qs ? `?${qs}` : ''}`, signal);
    },
  });
}

// ---------------------------------------------------------------- mutations

/** Upload + synchronous validate. Resolves to VALIDATED or FAILED_VALIDATION. */
export function useUploadBatch(): UseMutationResult<BatchOut, Error, File> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<BatchOut, Error, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append('file', file);
      return postForm<BatchOut>('/challan/batches', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['challan', 'batches'] });
    },
  });
}

/** Kick off reserve->render->issue in the background for a VALIDATED batch. */
export function useGenerateBatch(): UseMutationResult<
  BatchOut,
  Error,
  { batchId: number; series: string }
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<BatchOut, Error, { batchId: number; series: string }>({
    mutationFn: ({ batchId, series }) =>
      post<BatchOut>(`/challan/batches/${batchId}/generate`, { series }),
    onSuccess: (batch) => {
      qc.setQueryData(challanKeys.batch(batch.id), batch);
      void qc.invalidateQueries({ queryKey: ['challan', 'batches'] });
    },
  });
}

/** Void a challan + its bound number (ADMIN only server-side). */
export function useVoidChallan(): UseMutationResult<
  ChallanOut,
  Error,
  { challanId: number; reason: string }
> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<ChallanOut, Error, { challanId: number; reason: string }>({
    mutationFn: ({ challanId, reason }) =>
      post<ChallanOut>(`/challan/challans/${challanId}/void`, { reason }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['challan', 'challans'] });
    },
  });
}
