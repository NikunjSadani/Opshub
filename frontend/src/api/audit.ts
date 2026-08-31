import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  type InfiniteData,
  type UseInfiniteQueryResult,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { apiFetch, useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Audit & Access module.
 *
 * Mirrors the FROZEN backend contract (all paths relative to `/api/v1`, added by
 * `useApi()`; the whole surface is server-side gated on the `iam` platform
 * permission → 403 without it):
 *  - GET  /admin/audit/events    → paged activity trail (newest first) + CSV
 *  - GET  /admin/audit/logins    → paged login events (newest first) + CSV
 *  - GET  /admin/audit/integrity → tamper-evidence check over the trail
 *  - POST /auth/login-event      → record a successful explicit sign-in (204)
 */

// ------------------------------------------------------------------- shapes

/** The resolved actor/user on an audit record, or null when unresolved. */
export interface AuditPrincipal {
  email: string;
  name: string;
}

/** One audit-trail entry (backend GET /admin/audit/events item). */
export interface AuditEvent {
  id: number;
  /** ISO datetime string. */
  created_at: string;
  action: string;
  entity: string;
  entity_id: string | number | null;
  /** Free-form context — a string or a JSON object, depending on the action. */
  detail: unknown;
  /** The resolved actor, or null when it couldn't be resolved to a user. */
  actor: AuditPrincipal | null;
}

/** One login event (backend GET /admin/audit/logins item). */
export interface AuditLogin {
  id: number;
  /** ISO datetime string. */
  occurred_at: string;
  ip: string | null;
  user_agent: string | null;
  /** The resolved user, or null when unresolved. */
  user: AuditPrincipal | null;
}

/**
 * A paged response envelope. The contract documents `total | has_more` (a
 * count OR a boolean), so both are optional and the pager tolerates either —
 * or neither, falling back to "a full page might mean more".
 */
export interface AuditPage<T> {
  items: T[];
  total?: number;
  has_more?: boolean;
}

/** Result of the tamper-evidence check over the audit trail. */
export interface AuditIntegrity {
  intact: boolean;
  entries_checked: number;
  broken_at_id: number | null;
}

// ------------------------------------------------------------------ filters

export interface AuditEventFilters {
  /** Free-text actor match (email/name). */
  actor?: string;
  action?: string;
  entity?: string;
  /** Inclusive lower bound (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound (YYYY-MM-DD). */
  date_to?: string;
}

export interface AuditLoginFilters {
  user_id?: string;
  /** Inclusive lower bound (YYYY-MM-DD). */
  date_from?: string;
  /** Inclusive upper bound (YYYY-MM-DD). */
  date_to?: string;
}

/** Page size for both "Load more" pagers. */
export const AUDIT_PAGE_SIZE = 50;

// -------------------------------------------------------------- query builders

function appendEventFilters(params: URLSearchParams, f: AuditEventFilters): void {
  if (f.actor?.trim()) params.set('actor', f.actor.trim());
  if (f.action?.trim()) params.set('action', f.action.trim());
  if (f.entity?.trim()) params.set('entity', f.entity.trim());
  if (f.date_from?.trim()) params.set('date_from', f.date_from.trim());
  if (f.date_to?.trim()) params.set('date_to', f.date_to.trim());
}

function appendLoginFilters(params: URLSearchParams, f: AuditLoginFilters): void {
  if (f.user_id?.trim()) params.set('user_id', f.user_id.trim());
  if (f.date_from?.trim()) params.set('date_from', f.date_from.trim());
  if (f.date_to?.trim()) params.set('date_to', f.date_to.trim());
}

/** `?...&limit=&offset=` for the paged events list. */
export function buildAuditEventsQuery(f: AuditEventFilters, offset: number): string {
  const params = new URLSearchParams();
  appendEventFilters(params, f);
  params.set('limit', String(AUDIT_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

/** `?...&format=csv` for the events export (same filters, no paging). */
export function buildAuditEventsCsvQuery(f: AuditEventFilters): string {
  const params = new URLSearchParams();
  appendEventFilters(params, f);
  params.set('format', 'csv');
  return `?${params.toString()}`;
}

/** `?...&limit=&offset=` for the paged logins list. */
export function buildAuditLoginsQuery(f: AuditLoginFilters, offset: number): string {
  const params = new URLSearchParams();
  appendLoginFilters(params, f);
  params.set('limit', String(AUDIT_PAGE_SIZE));
  params.set('offset', String(offset));
  return `?${params.toString()}`;
}

/** `?...&format=csv` for the logins export (same filters, no paging). */
export function buildAuditLoginsCsvQuery(f: AuditLoginFilters): string {
  const params = new URLSearchParams();
  appendLoginFilters(params, f);
  params.set('format', 'csv');
  return `?${params.toString()}`;
}

// --------------------------------------------------------------- query keys
export const auditKeys = {
  events: (f: AuditEventFilters) => ['audit', 'events', f] as const,
  logins: (f: AuditLoginFilters) => ['audit', 'logins', f] as const,
  integrity: ['audit', 'integrity'] as const,
};

/**
 * Given the just-loaded page and all pages so far, decide the next offset.
 * Prefers the explicit signal the backend sends (`has_more`, else `total`),
 * and falls back to "a full page might mean more" when neither is present.
 */
function nextOffset<T>(lastPage: AuditPage<T>, allPages: AuditPage<T>[]): number | undefined {
  const loaded = allPages.reduce((n, p) => n + p.items.length, 0);
  if (typeof lastPage.has_more === 'boolean') return lastPage.has_more ? loaded : undefined;
  if (typeof lastPage.total === 'number') return loaded < lastPage.total ? loaded : undefined;
  return lastPage.items.length === AUDIT_PAGE_SIZE ? loaded : undefined;
}

// ------------------------------------------------------------------ queries

/** The audit activity trail, filtered, with offset-based "Load more" paging. */
export function useAuditEvents(
  filters: AuditEventFilters,
): UseInfiniteQueryResult<InfiniteData<AuditPage<AuditEvent>, number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    AuditPage<AuditEvent>,
    Error,
    InfiniteData<AuditPage<AuditEvent>, number>,
    readonly unknown[],
    number
  >({
    queryKey: auditKeys.events(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<AuditPage<AuditEvent>>(`/admin/audit/events${buildAuditEventsQuery(filters, pageParam)}`, signal),
    getNextPageParam: nextOffset,
  });
}

/** Login events, filtered by date, with offset-based "Load more" paging. */
export function useAuditLogins(
  filters: AuditLoginFilters,
): UseInfiniteQueryResult<InfiniteData<AuditPage<AuditLogin>, number>, Error> {
  const { get } = useApi();
  return useInfiniteQuery<
    AuditPage<AuditLogin>,
    Error,
    InfiniteData<AuditPage<AuditLogin>, number>,
    readonly unknown[],
    number
  >({
    queryKey: auditKeys.logins(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      get<AuditPage<AuditLogin>>(`/admin/audit/logins${buildAuditLoginsQuery(filters, pageParam)}`, signal),
    getNextPageParam: nextOffset,
  });
}

/** The tamper-evidence check over the audit trail (for the integrity badge). */
export function useAuditIntegrity(): UseQueryResult<AuditIntegrity, Error> {
  const { get } = useApi();
  return useQuery<AuditIntegrity, Error>({
    queryKey: auditKeys.integrity,
    queryFn: ({ signal }) => get<AuditIntegrity>('/admin/audit/integrity', signal),
  });
}

// ---------------------------------------------------------------- login ping

/**
 * Best-effort POST to record a successful explicit sign-in (backend reads IP +
 * user-agent from the request; responds 204). Used by the Firebase auth
 * provider straight after `signInWithEmailAndPassword` succeeds. It may throw
 * (network/403); the caller SWALLOWS so the audit ping never blocks or fails a
 * login. Never called on a restored session.
 */
export async function recordLoginEvent(token: string | null): Promise<void> {
  await apiFetch<void>('/auth/login-event', { method: 'POST', token });
}

/**
 * Mutation form of {@link recordLoginEvent}, for callers already inside the
 * auth context (and for testing that the ping targets POST /auth/login-event).
 */
export function useRecordLoginEvent(): UseMutationResult<void, ApiError, void> {
  const { post } = useApi();
  return useMutation<void, ApiError, void>({
    mutationFn: () => post<void>('/auth/login-event'),
  });
}
