import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
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
  /** Inclusive lower bound on challan_date (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound on challan_date (YYYY-MM-DD). */
  date_to?: string;
}

/** Filters for the aggregate summary (no status — it splits ISSUED vs VOID itself). */
export interface ChallanSummaryFilters {
  series?: string;
  fy?: string;
}

/** One row of the summary's per-series/fy breakdown (backend SeriesBreakdownOut). */
export interface SeriesBreakdown {
  series: string;
  fy: string;
  issued: number;
  void: number;
  /** PAISE; sum over ISSUED rows only (value-free rows excluded). */
  total_value_paise: number;
}

/** Aggregate counts + value over the register (backend ChallanSummaryOut). */
export interface ChallanSummary {
  issued_count: number;
  void_count: number;
  /** ISSUED with eway_required. */
  eway_count: number;
  /** ISSUED with a value (total_paise not null). */
  valued_count: number;
  /** PAISE; sum over ISSUED, value-free rows excluded. */
  total_value_paise: number;
  by_series: SeriesBreakdown[];
}

/** Register page size for the "Load more" pager. */
export const CHALLAN_PAGE_SIZE = 100;

/**
 * Build a `?series=&fy=` query string, appending only non-empty (trimmed) params.
 * Exported + pure so it can be unit-tested without a hook.
 */
export function buildChallanSummaryQuery(filters: ChallanSummaryFilters): string {
  const params = new URLSearchParams();
  if (filters.series?.trim()) params.set('series', filters.series.trim());
  if (filters.fy?.trim()) params.set('fy', filters.fy.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

/**
 * Append the shared register filters (series / fy / status / date_from / date_to)
 * to a params bag, each only when non-empty (trimmed). Used by both the paged list
 * query and the CSV export so their filter semantics stay identical.
 */
function appendChallanFilters(params: URLSearchParams, filters: ChallanFilters): void {
  if (filters.series?.trim()) params.set('series', filters.series.trim());
  if (filters.fy?.trim()) params.set('fy', filters.fy.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
}

/**
 * Build a `?series=&fy=&status=&date_from=&date_to=&limit=&offset=` query string
 * for the register. Filter params are appended only when non-empty (trimmed);
 * limit/offset always.
 */
export function buildChallanListQuery(filters: ChallanFilters, offset: number): string {
  const params = new URLSearchParams();
  appendChallanFilters(params, filters);
  params.set('limit', String(CHALLAN_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

/**
 * Build the query string for the CSV export — the same filters as the register
 * list (series / fy / status / date_from / date_to) but no limit/offset paging.
 * Returns '' when no filters are set.
 */
export function buildChallanCsvQuery(filters: ChallanFilters): string {
  const params = new URLSearchParams();
  appendChallanFilters(params, filters);
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --------------------------------------------------------------- query keys
export const challanKeys = {
  batches: (limit: number) => ['challan', 'batches', limit] as const,
  batch: (id: number) => ['challan', 'batch', id] as const,
  challans: (filters: ChallanFilters) => ['challan', 'challans', filters] as const,
  summary: (filters: ChallanSummaryFilters) => ['challan', 'summary', filters] as const,
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

/**
 * The challan register, filtered by series / fy / status, with offset-based
 * "Load more" paging. Each page fetches up to `CHALLAN_PAGE_SIZE` rows; there is
 * another page only when the last one came back exactly full (a short page means
 * the end). The offset for the next page is the running total already loaded.
 */
export function useChallansInfiniteQuery(
  filters: ChallanFilters,
): UseInfiniteQueryResult<InfiniteData<ChallanOut[], number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    ChallanOut[],
    Error,
    InfiniteData<ChallanOut[], number>,
    readonly unknown[],
    number
  >({
    queryKey: challanKeys.challans(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<ChallanOut[]>(`/challan/challans${buildChallanListQuery(filters, pageParam)}`, signal),
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === CHALLAN_PAGE_SIZE
        ? allPages.reduce((total, page) => total + page.length, 0)
        : undefined,
  });
}

/** Aggregate register summary (counts + value + per-series breakdown). */
export function useChallanSummaryQuery(
  filters: ChallanSummaryFilters,
): UseQueryResult<ChallanSummary, Error> {
  const { get } = useApi();
  return useQuery<ChallanSummary, Error>({
    queryKey: challanKeys.summary(filters),
    queryFn: ({ signal }) =>
      get<ChallanSummary>(`/challan/summary${buildChallanSummaryQuery(filters)}`, signal),
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
