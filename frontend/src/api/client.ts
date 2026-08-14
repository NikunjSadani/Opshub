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

/**
 * Extract a human message from a FastAPI error body. `detail` is a plain string
 * for our HTTPExceptions (409/403/404/413) but a LIST of `{loc,msg}` for 422
 * validation errors — `String()`-ing that list yields "[object Object]", so we
 * join the messages (prefixed by the offending field) instead.
 */
export function detailMessage(payload: unknown): string | undefined {
  if (!payload || typeof payload !== 'object' || !('detail' in payload)) return undefined;
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((e) => {
        if (e && typeof e === 'object' && 'msg' in e) {
          const loc = 'loc' in e && Array.isArray((e as { loc: unknown[] }).loc)
            ? (e as { loc: unknown[] }).loc
            : [];
          const field = loc.length ? String(loc[loc.length - 1]) : '';
          const msg = String((e as { msg: unknown }).msg);
          return field && field !== 'body' ? `${field}: ${msg}` : msg;
        }
        return undefined;
      })
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  return undefined;
}

/** Build an ApiError from a failed download Response, preferring the FastAPI detail. */
async function downloadError(res: Response): Promise<ApiError> {
  let message = `Download failed: ${res.status}`;
  if (res.headers.get('content-type')?.includes('application/json')) {
    try {
      message = detailMessage(await res.json()) ?? message;
    } catch {
      /* keep the fallback message */
    }
  }
  return new ApiError(res.status, message);
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
    const message = detailMessage(payload) ?? `Request failed: ${res.status} ${res.statusText}`;
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

  const download = useCallback(
    async (fileId: number, fallbackName = 'download') => {
      const token = await getToken();
      const res = await fetch(`${API_BASE}/files/${fileId}/download`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      if (!res.ok) throw await downloadError(res);
      const blob = await res.blob();
      const disposition = res.headers.get('content-disposition') ?? '';
      const match = /filename="?([^"]+)"?/.exec(disposition);
      const name = match?.[1] ?? fallbackName;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
    [getToken],
  );

  const downloadUrl = useCallback(
    async (path: string, fallbackName = 'download') => {
      const token = await getToken();
      const res = await fetch(`${API_BASE}${path}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      if (!res.ok) throw await downloadError(res);
      const blob = await res.blob();
      const disposition = res.headers.get('content-disposition') ?? '';
      const match = /filename="?([^"]+)"?/.exec(disposition);
      const name = match?.[1] ?? fallbackName;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
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
      <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
      [request],
    ),
    put: useCallback(
      <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body }),
      [request],
    ),
    patch: useCallback(
      <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body }),
      [request],
    ),
    del: useCallback(<T>(path: string) => request<T>(path, { method: 'DELETE' }), [request]),
    /** Fetch an authed file (by id) and trigger a browser download. */
    download,
    /** Fetch an authed URL (relative to /api/v1) as a blob and trigger a download. */
    downloadUrl,
    /** POST a multipart form (e.g. the challan xlsx upload). */
    postForm: useCallback(
      async <T>(path: string, form: FormData): Promise<T> => {
        const token = await getToken();
        const res = await fetch(`${API_BASE}${path}`, {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          body: form,
        });
        const isJson = res.headers.get('content-type')?.includes('application/json');
        const payload: unknown = isJson ? await res.json().catch(() => undefined) : undefined;
        if (!res.ok) {
          throw new ApiError(res.status, detailMessage(payload) ?? `Upload failed: ${res.status}`, payload);
        }
        return payload as T;
      },
      [getToken],
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
