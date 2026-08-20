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
  /** 10-char PAN, or null when not captured. */
  pan: string | null;
  /** Payment/credit terms in days, or null when not set. */
  credit_terms_days: number | null;
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

// ===========================================================================
// Client Master — a client's full profile (PAN / credit terms) plus its child
// collections (GSTINs, addresses, contacts). Reads need projects VIEW; every
// mutation is a `client.manage` action = projects MANAGE (server-enforced).
// ===========================================================================

export interface Gstin {
  id: string;
  /** 15-char GSTIN. */
  gstin: string;
  legal_name: string | null;
  /** 2-digit state code prefix of the GSTIN. */
  state_code: string | null;
  is_default: boolean;
  active: boolean;
}

export interface Address {
  id: string;
  /** Optional GSTIN this address is registered under. */
  gstin_id: string | null;
  label: string | null;
  line1: string;
  line2: string | null;
  city: string | null;
  state: string | null;
  pincode: string | null;
  is_default: boolean;
  active: boolean;
}

export interface Contact {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  designation: string | null;
  is_default: boolean;
  active: boolean;
}

/** A client's full profile — header fields + ACTIVE children (backend ClientDetailOut). */
export interface ClientDetail {
  id: string;
  name: string;
  code: string;
  pan: string | null;
  credit_terms_days: number | null;
  active: boolean;
  gstins: Gstin[];
  addresses: Address[];
  contacts: Contact[];
}

/** Editable client header fields (all optional — send only what changed). */
export interface ClientPatch {
  name?: string;
  pan?: string | null;
  credit_terms_days?: number | null;
  active?: boolean;
}

export interface GstinInput {
  gstin: string;
  legal_name?: string;
  state_code?: string;
  is_default?: boolean;
}
export interface GstinPatch {
  legal_name?: string;
  state_code?: string;
  is_default?: boolean;
}

export interface AddressInput {
  gstin_id?: string;
  label?: string;
  line1: string;
  line2?: string;
  city?: string;
  state?: string;
  pincode?: string;
  is_default?: boolean;
}
export type AddressPatch = Partial<AddressInput>;

export interface ContactInput {
  name: string;
  email?: string;
  phone?: string;
  designation?: string;
  is_default?: boolean;
}
export type ContactPatch = Partial<ContactInput>;

// Client-detail query key. It nests UNDER `projectKeys.clients` so invalidating the
// client list (`['projects','clients']`) also refreshes any loaded detail by prefix.
export const clientKeys = {
  detail: (id: string) => [...projectKeys.clients, 'detail', id] as const,
};

/** A single client's full profile (header + active children). Needs projects VIEW. */
export function useClientDetail(id: string | null): UseQueryResult<ClientDetail, Error> {
  const { get } = useApi();
  return useQuery<ClientDetail, Error>({
    queryKey: clientKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<ClientDetail>(`/projects/clients/${id}`, signal),
  });
}

/**
 * Invalidate a client's detail AND the client list on any client-master mutation.
 * The detail key is a prefix-child of the list key, so this refreshes both the
 * open profile and the list's header columns (pan / credit terms / default flags).
 */
function useInvalidateClient(clientId: string): () => void {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: clientKeys.detail(clientId) });
    void qc.invalidateQueries({ queryKey: projectKeys.clients });
  };
}

/** Edit a client's header (name / pan / credit terms / active). client.manage. */
export function useUpdateClient(clientId: string): UseMutationResult<Client, ApiError, ClientPatch> {
  const { patch } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Client, ApiError, ClientPatch>({
    mutationFn: (body) => patch<Client>(`/projects/clients/${clientId}`, body),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------------------------- GSTINs
export function useAddGstin(clientId: string): UseMutationResult<Gstin, ApiError, GstinInput> {
  const { post } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Gstin, ApiError, GstinInput>({
    mutationFn: (body) => post<Gstin>(`/projects/clients/${clientId}/gstins`, body),
    onSuccess: invalidate,
  });
}
export function useUpdateGstin(
  clientId: string,
): UseMutationResult<Gstin, ApiError, { gstin_id: string; patch: GstinPatch }> {
  const { patch } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Gstin, ApiError, { gstin_id: string; patch: GstinPatch }>({
    mutationFn: ({ gstin_id, patch: body }) => patch<Gstin>(`/projects/clients/gstins/${gstin_id}`, body),
    onSuccess: invalidate,
  });
}
export function useDeactivateGstin(clientId: string): UseMutationResult<void, ApiError, string> {
  const { del } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<void, ApiError, string>({
    mutationFn: (gstinId) => del<void>(`/projects/clients/gstins/${gstinId}`),
    onSuccess: invalidate,
  });
}

// ----------------------------------------------------------------- Addresses
export function useAddAddress(clientId: string): UseMutationResult<Address, ApiError, AddressInput> {
  const { post } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Address, ApiError, AddressInput>({
    mutationFn: (body) => post<Address>(`/projects/clients/${clientId}/addresses`, body),
    onSuccess: invalidate,
  });
}
export function useUpdateAddress(
  clientId: string,
): UseMutationResult<Address, ApiError, { address_id: string; patch: AddressPatch }> {
  const { patch } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Address, ApiError, { address_id: string; patch: AddressPatch }>({
    mutationFn: ({ address_id, patch: body }) =>
      patch<Address>(`/projects/clients/addresses/${address_id}`, body),
    onSuccess: invalidate,
  });
}
export function useDeactivateAddress(clientId: string): UseMutationResult<void, ApiError, string> {
  const { del } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<void, ApiError, string>({
    mutationFn: (addressId) => del<void>(`/projects/clients/addresses/${addressId}`),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------------------------ Contacts
export function useAddContact(clientId: string): UseMutationResult<Contact, ApiError, ContactInput> {
  const { post } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Contact, ApiError, ContactInput>({
    mutationFn: (body) => post<Contact>(`/projects/clients/${clientId}/contacts`, body),
    onSuccess: invalidate,
  });
}
export function useUpdateContact(
  clientId: string,
): UseMutationResult<Contact, ApiError, { contact_id: string; patch: ContactPatch }> {
  const { patch } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<Contact, ApiError, { contact_id: string; patch: ContactPatch }>({
    mutationFn: ({ contact_id, patch: body }) =>
      patch<Contact>(`/projects/clients/contacts/${contact_id}`, body),
    onSuccess: invalidate,
  });
}
export function useDeactivateContact(clientId: string): UseMutationResult<void, ApiError, string> {
  const { del } = useApi();
  const invalidate = useInvalidateClient(clientId);
  return useMutation<void, ApiError, string>({
    mutationFn: (contactId) => del<void>(`/projects/clients/contacts/${contactId}`),
    onSuccess: invalidate,
  });
}
