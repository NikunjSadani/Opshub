import { useCallback } from 'react';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthProvider';
import type { ModuleDescriptor } from '../types/modules';

/** All API calls are relative to this base and served same-origin in prod. */
export const API_BASE = '/api/v1';

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export interface RequestOptions {
  method?: string;
  /** JSON-serializable request body. */
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
}

/**
 * Low-level typed fetch wrapper. Attaches `Authorization: Bearer <token>` when a
 * token is provided, sends/parses JSON, and throws `ApiError` on non-2xx.
 * Stateless by design so it is trivial to unit-test.
 */
export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;

  const res = await fetch(`${API_BASE}${path}`, {
    method: opts.method ?? 'GET',
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const payload: unknown = isJson ? await res.json().catch(() => undefined) : undefined;

  if (!res.ok) {
    const message =
      (payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : undefined) ?? `Request failed: ${res.status} ${res.statusText}`;
    throw new ApiError(res.status, message, payload);
  }

  return payload as T;
}

/**
 * Returns an authed fetcher bound to the current user's token. Resolves the
 * token from `useAuth().getToken()` on every call (so refreshed tokens are
 * always used), then delegates to `apiFetch`.
 */
export function useApi() {
  const { getToken } = useAuth();

  const request = useCallback(
    async <T>(path: string, opts: Omit<RequestOptions, 'token'> = {}): Promise<T> => {
      const token = await getToken();
      return apiFetch<T>(path, { ...opts, token });
    },
    [getToken],
  );

  return {
    request,
    get: useCallback(
      <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
      [request],
    ),
    post: useCallback(
      <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body }),
      [request],
    ),
  };
}

/** React Query hook for the sidebar/dashboard module list. */
export function useModules(): UseQueryResult<ModuleDescriptor[], Error> {
  const { get } = useApi();
  return useQuery<ModuleDescriptor[], Error>({
    queryKey: ['modules'],
    queryFn: ({ signal }) => get<ModuleDescriptor[]>('/modules', signal),
    staleTime: 5 * 60 * 1000,
  });
}
