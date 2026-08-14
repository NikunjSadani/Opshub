import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Projects module.
 * Mirrors the backend Projects routes (ClientOut / ProjectOut). All paths are
 * relative to `/api/v1` (added by `useApi()`).
 *
 * Ids are treated as strings: they only ever flow through `<select>` values,
 * query strings, and equality checks — never arithmetic — so a string is the
 * safe, UI-correct representation regardless of the backend's id kind.
 */

// --- status union (backend ProjectStatus) ---
export type ProjectStatus = 'ACTIVE' | 'ON_HOLD' | 'CLOSED';

/** All statuses, in display order (used by filters + the status control). */
export const PROJECT_STATUSES: readonly ProjectStatus[] = ['ACTIVE', 'ON_HOLD', 'CLOSED'];

export interface Client {
  id: string;
  name: string;
  /** Unique 3-letter uppercase code, e.g. "BRI". */
  code: string;
  active: boolean;
}

export interface ClientInput {
  name: string;
  code: string;
}

export interface Project {
  id: string;
  /** Server-assigned `<CLIENT_CODE>-<running number>`, e.g. "BRI-001". */
  code: string;
  client_id: string;
  client_code: string;
  client_name: string;
  name: string;
  /** ISO date string (backend `date`), or null when not set. */
  start_date: string | null;
  status: ProjectStatus;
  description: string | null;
  /** ISO datetime string (backend `datetime`). */
  created_at: string;
}

export interface ProjectInput {
  client_id: string;
  name: string;
  /** Optional YYYY-MM-DD. */
  start_date?: string;
  description?: string;
}

export interface ProjectFilters {
  client_id?: string;
  status?: ProjectStatus | '';
  /** Free-text search over code / name. */
  q?: string;
}

/**
 * Build a `?client_id=&status=&q=` query string for the projects list, appending
 * only non-empty (trimmed) params. Exported + pure so it can be unit-tested
 * without a hook.
 */
export function buildProjectsQuery(filters: ProjectFilters): string {
  const params = new URLSearchParams();
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --------------------------------------------------------------- query keys
export const projectKeys = {
  clients: ['projects', 'clients'] as const,
  list: (filters: ProjectFilters) => ['projects', 'list', filters] as const,
  detail: (id: string) => ['projects', 'detail', id] as const,
};

// ------------------------------------------------------------------ queries

/** All registered clients (name + unique code). */
export function useClientsQuery(): UseQueryResult<Client[], Error> {
  const { get } = useApi();
  return useQuery<Client[], Error>({
    queryKey: projectKeys.clients,
    queryFn: ({ signal }) => get<Client[]>('/projects/clients', signal),
  });
}

/** Projects (newest first), filtered by client / status / free-text. */
export function useProjectsQuery(filters: ProjectFilters): UseQueryResult<Project[], Error> {
  const { get } = useApi();
  return useQuery<Project[], Error>({
    queryKey: projectKeys.list(filters),
    queryFn: ({ signal }) => get<Project[]>(`/projects${buildProjectsQuery(filters)}`, signal),
  });
}

/** A single project by id. */
export function useProjectQuery(id: string | null): UseQueryResult<Project, Error> {
  const { get } = useApi();
  return useQuery<Project, Error>({
    queryKey: projectKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<Project>(`/projects/${id}`, signal),
  });
}

// ---------------------------------------------------------------- mutations

/** Register a client (ADMIN). Invalidates the client list on success. */
export function useCreateClient(): UseMutationResult<Client, ApiError, ClientInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Client, ApiError, ClientInput>({
    mutationFn: (body) => post<Client>('/projects/clients', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: projectKeys.clients });
    },
  });
}

/** Create a project (code auto-generated). Invalidates the project list on success. */
export function useCreateProject(): UseMutationResult<Project, ApiError, ProjectInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Project, ApiError, ProjectInput>({
    mutationFn: (body) => post<Project>('/projects', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects', 'list'] });
    },
  });
}

/** Change a project's status (ADMIN). Refreshes the list + that project's detail. */
export function useUpdateProjectStatus(): UseMutationResult<
  Project,
  ApiError,
  { id: string; status: ProjectStatus }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<Project, ApiError, { id: string; status: ProjectStatus }>({
    mutationFn: ({ id, status }) => patch<Project>(`/projects/${id}`, { status }),
    onSuccess: (project) => {
      qc.setQueryData(projectKeys.detail(project.id), project);
      void qc.invalidateQueries({ queryKey: ['projects', 'list'] });
    },
  });
}
