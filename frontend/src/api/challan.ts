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
  | 'NEEDS_REVIEW'
  | 'GENERATING'
  | 'COMPLETED'
  | 'FAILED';

export type ChallanStatus = 'ISSUED' | 'VOID';

/** A batch is done rendering (no more polling) when it reaches one of these. */
const TERMINAL_BATCH: readonly BatchStatus[] = [
  'PENDING',
  'FAILED_VALIDATION',
  'VALIDATED',
  'NEEDS_REVIEW',
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

/**
 * One consignee contradiction awaiting an operator decision on a NEEDS_REVIEW
 * batch: the uploaded value for `field` disagrees with the stored golden record
 * for `gstin`. `choice` starts "PENDING" and becomes one of DecisionChoice.
 */
export interface Decision {
  id: number;
  gstin: string;
  consignee_name: string;
  /** One of name|address_line1|address_line2|pincode|state|phone. */
  field: string;
  stored_value: string;
  uploaded_value: string;
  choice: string;
}

/** How the operator resolves a single consignee contradiction. */
export type DecisionChoice = 'UPDATE_MASTER' | 'THIS_UPLOAD' | 'REJECT';

export interface ChallanOut {
  id: number;
  number: string;
  series: string;
  fy: string;
  /** ISO date string (backend `date`). */
  challan_date: string;
  /** The referenced Project ID (backend `project_code`, e.g. "BRI-001"). */
  project_code: string;
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

// --------------------------------------------------------- bulk PDF download

/** One challan the spec resolved to (backend download-preview `resolved` row). */
export interface DownloadResolved {
  /** The numeric part of the challan number, scoped to (series, fy). */
  number_int: number;
  /** The full formatted number, e.g. "L/26-27/0010". */
  number: string;
  id: number;
}

/**
 * The result of previewing a bulk-download spec against a (series, fy): what
 * would download, what is skipped because it is void, what wasn't found, and any
 * parse/validation errors for the spec itself. Mirrors the backend
 * `GET /challan/download/preview` response.
 */
export interface DownloadPreview {
  series: string;
  fy: string;
  /** How many challans would actually be included in the download. */
  count: number;
  resolved: DownloadResolved[];
  /** Numbers that exist but are VOID — deliberately excluded; tell the operator. */
  skipped_void: number[];
  /** ISSUED numbers with no rendered PDF yet — can't download; reported so count is honest. */
  no_pdf: number[];
  /** Numbers in the spec with no matching challan in (series, fy). */
  missing: number[];
  /** Human-readable problems with the spec (e.g. an unparsable range). */
  errors: string[];
}

/** The identifying inputs for a bulk download: a (series, fy) and a numbers spec. */
export interface DownloadParams {
  series: string;
  fy: string;
  /** A range and/or comma-separated list, e.g. "10-50, 55, 60". */
  spec: string;
}

/** How the bulk PDFs are packaged. */
export type DownloadMode = 'separate' | 'merged';

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

/**
 * Build a `?series=&fy=&spec=[&mode=]` query string for the bulk-download preview
 * and download endpoints. `URLSearchParams` URL-encodes the spec (its commas and
 * spaces) for us. Exported + pure so it can be unit-tested without a hook.
 */
export function buildDownloadQuery(params: DownloadParams, mode?: DownloadMode): string {
  const p = new URLSearchParams();
  p.set('series', params.series.trim());
  p.set('fy', params.fy.trim());
  p.set('spec', params.spec.trim());
  if (mode) p.set('mode', mode);
  return `?${p.toString()}`;
}

// --------------------------------------------------------------- query keys
export const challanKeys = {
  batches: (limit: number) => ['challan', 'batches', limit] as const,
  batch: (id: number) => ['challan', 'batch', id] as const,
  decisions: (id: number) => ['challan', 'decisions', id] as const,
  challans: (filters: ChallanFilters) => ['challan', 'challans', filters] as const,
  summary: (filters: ChallanSummaryFilters) => ['challan', 'summary', filters] as const,
  downloadPreview: (params: DownloadParams | null) =>
    ['challan', 'download-preview', params] as const,
};

// ------------------------------------------------------------------ queries

/**
 * Recent batches (newest first). Self-polls (default every 2.5s) while ANY row
 * is GENERATING so its artifacts appear without a manual refresh — exactly what
 * New Challan promises ("come back to the Batches tab later"). Polling stops the
 * moment no row is GENERATING, so a quiet list makes no background requests.
 */
export function useBatchesQuery(limit = 50, pollMs = 2500): UseQueryResult<BatchOut[], Error> {
  const { get } = useApi();
  return useQuery<BatchOut[], Error>({
    queryKey: challanKeys.batches(limit),
    queryFn: ({ signal }) => get<BatchOut[]>(`/challan/batches?limit=${limit}`, signal),
    refetchInterval: (query) =>
      query.state.data?.some((b) => b.status === 'GENERATING') ? pollMs : false,
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
 * The consignee contradictions for a NEEDS_REVIEW batch, each awaiting a
 * decision. Enabled only once a batch id is known; not polled (a batch's
 * decisions change only via the operator's own PATCH, which invalidates this).
 */
export function useBatchDecisions(
  batchId: number | null,
): UseQueryResult<Decision[], Error> {
  const { get } = useApi();
  return useQuery<Decision[], Error>({
    queryKey: challanKeys.decisions(batchId ?? -1),
    enabled: batchId != null,
    queryFn: ({ signal }) =>
      get<Decision[]>(`/challan/batches/${batchId}/decisions`, signal),
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
      // Only offer another page when the last one came back EXACTLY full and was
      // non-empty. A short OR empty page means the end, so we never chase one
      // wasted empty fetch past the last row (e.g. a total that is a multiple of
      // the page size still stops cleanly on the following empty page).
      lastPage.length > 0 && lastPage.length === CHALLAN_PAGE_SIZE
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

/**
 * Preview a bulk-download spec against a (series, fy). Enabled only once a caller
 * has committed a full set of params (series + fy + spec, all non-empty) — the
 * screen sets these on the operator's explicit "Preview" click, so each keystroke
 * does NOT fire a request. Not polled; a spec's resolution changes only when the
 * operator submits a new one (a new query key). The body carries its own
 * `errors[]` for a malformed spec — those come back on a 200, not as a throw, so
 * the caller renders them as inline validation rather than an error state.
 */
export function useDownloadPreview(
  params: DownloadParams | null,
): UseQueryResult<DownloadPreview, Error> {
  const { get } = useApi();
  return useQuery<DownloadPreview, Error>({
    queryKey: challanKeys.downloadPreview(params),
    enabled: params != null,
    queryFn: ({ signal }) =>
      get<DownloadPreview>(`/challan/download/preview${buildDownloadQuery(params!)}`, signal),
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

/**
 * Reconcile a batch STUCK in GENERATING (e.g. a crash/deploy mid-run) back to
 * FAILED so it can be retried: issued challans are kept, un-issued reservations
 * voided (server-side). ADMIN only. Returns the updated batch; on success we
 * seed its cache and refresh the recent-batches list so the row leaves
 * GENERATING and its "Retry generation" action becomes available.
 */
export function useRecoverBatch(): UseMutationResult<BatchOut, Error, { batchId: number }> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<BatchOut, Error, { batchId: number }>({
    mutationFn: ({ batchId }) => post<BatchOut>(`/challan/batches/${batchId}/recover`, {}),
    onSuccess: (batch) => {
      qc.setQueryData(challanKeys.batch(batch.id), batch);
      void qc.invalidateQueries({ queryKey: ['challan', 'batches'] });
    },
  });
}

/**
 * Resolve consignee contradictions on a NEEDS_REVIEW batch. Submits a subset or
 * all of the decisions; when none remain PENDING the batch flips to VALIDATED.
 * Invalidates the batch, its decisions, and the recent-batches list on success.
 */
export function useSubmitDecisions(): UseMutationResult<
  BatchOut,
  Error,
  { batchId: number; decisions: { id: number; choice: DecisionChoice }[] }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<
    BatchOut,
    Error,
    { batchId: number; decisions: { id: number; choice: DecisionChoice }[] }
  >({
    mutationFn: ({ batchId, decisions }) =>
      patch<BatchOut>(`/challan/batches/${batchId}/decisions`, { decisions }),
    onSuccess: (batch) => {
      qc.setQueryData(challanKeys.batch(batch.id), batch);
      void qc.invalidateQueries({ queryKey: challanKeys.decisions(batch.id) });
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
