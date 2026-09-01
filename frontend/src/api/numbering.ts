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
 * Typed contracts + React Query hooks for the Numbering register.
 * Mirrors backend GET /numbering/allocations and GET /numbering/counters.
 * All paths are relative to `/api/v1` (added by `useApi()`).
 */

// --- status union (backend allocation status) ---
export type AllocationStatus = 'RESERVED' | 'ISSUED' | 'VOID';

/** One allocated number and what (if anything) it is bound to. */
export interface Allocation {
  id: number;
  series: string;
  fy: string;
  number: number;
  formatted: string;
  status: AllocationStatus;
  /** The entity type a number is bound to (e.g. "challan"), or null. */
  entity: string | null;
  /** The bound entity's id, or null when unbound. */
  entity_id: number | null;
  void_reason: string | null;
}

/** High-water mark (last issued number) per series/fy. */
export interface Counter {
  series: string;
  fy: string;
  last_number: number;
}

export interface AllocationFilters {
  series?: string;
  fy?: string;
  status?: AllocationStatus | '';
}

/** Register page size for the "Load more" pager (backend caps `limit` at 500). */
export const ALLOCATION_PAGE_SIZE = 100;

/**
 * Build a `?series=&fy=&status=&limit=&offset=` query string for the allocations
 * register. Filter params are appended only when non-empty (trimmed);
 * limit/offset always. Exported + pure so it can be unit-tested without a hook.
 */
export function buildAllocationListQuery(filters: AllocationFilters, offset: number): string {
  const params = new URLSearchParams();
  if (filters.series?.trim()) params.set('series', filters.series.trim());
  if (filters.fy?.trim()) params.set('fy', filters.fy.trim());
  if (filters.status) params.set('status', filters.status);
  params.set('limit', String(ALLOCATION_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

// --------------------------------------------------------------- query keys
export const numberingKeys = {
  allocations: (filters: AllocationFilters) => ['numbering', 'allocations', filters] as const,
  counters: () => ['numbering', 'counters'] as const,
};

// ------------------------------------------------------------------ queries

/**
 * The allocations register, filtered by series / fy / status, with offset-based
 * "Load more" paging. Each page fetches up to `ALLOCATION_PAGE_SIZE` rows; there is
 * another page only when the last one came back exactly full (a short page means
 * the end). The offset for the next page is the running total already loaded.
 */
export function useAllocationsQuery(
  filters: AllocationFilters,
): UseInfiniteQueryResult<InfiniteData<Allocation[], number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    Allocation[],
    Error,
    InfiniteData<Allocation[], number>,
    readonly unknown[],
    number
  >({
    queryKey: numberingKeys.allocations(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<Allocation[]>(`/numbering/allocations${buildAllocationListQuery(filters, pageParam)}`, signal),
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === ALLOCATION_PAGE_SIZE
        ? allPages.reduce((total, page) => total + page.length, 0)
        : undefined,
  });
}

/** Per-series/fy high-water marks. */
export function useCountersQuery(): UseQueryResult<Counter[], Error> {
  const { get } = useApi();
  return useQuery<Counter[], Error>({
    queryKey: numberingKeys.counters(),
    queryFn: ({ signal }) => get<Counter[]>('/numbering/counters', signal),
  });
}

// ---------------------------------------------------------------- mutations

export interface SeedSeriesInput {
  series: string;
  /** FY like "26-27"; omit/blank for the current financial year. */
  fy?: string;
  /** High-water mark = last issued number; the next issue is this + 1 (0 = fresh series). */
  last_number: number;
}

/**
 * Seed (configure) the starting number for a (series, fy) — mirrors backend
 * `POST /numbering/seed`. Sets the high-water mark so the next allocated challan
 * is `last_number + 1`. Requires MANAGE on the Delivery Challan module; the
 * backend additionally guards it so it can NEVER drop below a number already
 * issued (no reissue risk). Refreshes the counters grid on success.
 */
export function useSeedSeries(): UseMutationResult<Counter, Error, SeedSeriesInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Counter, Error, SeedSeriesInput>({
    mutationFn: ({ series, fy, last_number }) =>
      post<Counter>('/numbering/seed', {
        series,
        ...(fy && fy.trim() ? { fy: fy.trim() } : {}),
        last_number,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: numberingKeys.counters() });
    },
  });
}
