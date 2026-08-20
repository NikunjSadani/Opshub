import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the User Management module (RBAC v2).
 * Mirrors the backend Users routes (UserOut). All paths are relative to
 * `/api/v1` (added by `useApi()`), bearer auto-attached.
 *
 * RBAC v2: a user is assigned ONE named Role (managed on the Roles screen).
 * There is no longer a fixed role enum or per-user module grants — the user
 * forms pick a role from the list of existing roles.
 *
 * NEVER models or transmits a password — accounts are provisioned via a
 * one-time setup link the admin passes to the user out-of-band.
 */

/** A user as served by `GET /users`. */
export interface UserOut {
  id: number;
  email: string;
  name: string;
  /** The assigned role's id, or null when the user has no role yet. */
  role_id: number | null;
  /** The assigned role's display name, or null when the user has no role. */
  role_name: string | null;
  active: boolean;
  /** True once the user has set a password (finished setup). */
  is_provisioned: boolean;
  /** ISO datetime string. */
  created_at: string;
}

/** Body for `POST /users`. */
export interface CreateUserInput {
  email: string;
  name: string;
  role_id: number;
}

/** `POST /users` response: the created user + a one-time setup link (or null). */
export interface CreateUserResult {
  user: UserOut;
  /** Password-setup link to hand to the user, or null when auth isn't configured. */
  setup_link: string | null;
}

/** Body for `PATCH /users/{id}` — every field optional (partial update). */
export interface UpdateUserInput {
  name?: string;
  role_id?: number;
  active?: boolean;
}

/** `POST /users/{id}/setup-link` response. */
export interface SetupLinkResult {
  setup_link: string | null;
}

/** A role as served by `GET /roles` (only the fields this screen needs). */
export interface RoleOut {
  id: number;
  name: string;
  description: string | null;
  is_system: boolean;
}

/** A role reduced to what the picker needs: id (value) + name (label). */
export interface AssignableRole {
  id: number;
  name: string;
}

// --------------------------------------------------------------- query keys
export const userKeys = {
  list: ['users', 'list'] as const,
  roles: ['users', 'assignable-roles'] as const,
};

// ------------------------------------------------------------------ queries

/** All users (as served by the backend, ADMIN-only server-side). */
export function useUsers(): UseQueryResult<UserOut[], Error> {
  const { get } = useApi();
  return useQuery<UserOut[], Error>({
    queryKey: userKeys.list,
    queryFn: ({ signal }) => get<UserOut[]>('/users', signal),
  });
}

/**
 * The roles an admin may assign, for the role picker. Wraps `GET /roles` and
 * reduces each to `{ id, name }`. Defined locally (rather than importing from a
 * sibling `api/roles.ts` that may not exist yet) so User Management has no hard
 * dependency on the Roles screen's module.
 */
export function useAssignableRoles(): UseQueryResult<AssignableRole[], Error> {
  const { get } = useApi();
  return useQuery<RoleOut[], Error, AssignableRole[]>({
    queryKey: userKeys.roles,
    queryFn: ({ signal }) => get<RoleOut[]>('/roles', signal),
    staleTime: 5 * 60 * 1000,
    select: (roles) => roles.map(({ id, name }) => ({ id, name })),
  });
}

// ---------------------------------------------------------------- mutations

/** Invite a user (ADMIN). Invalidates the user list on success. */
export function useCreateUser(): UseMutationResult<CreateUserResult, ApiError, CreateUserInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<CreateUserResult, ApiError, CreateUserInput>({
    mutationFn: (body) => post<CreateUserResult>('/users', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userKeys.list });
    },
  });
}

/** Update a user's name/role/active (ADMIN). Refreshes the list. */
export function useUpdateUser(): UseMutationResult<
  UserOut,
  ApiError,
  { id: number } & UpdateUserInput
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<UserOut, ApiError, { id: number } & UpdateUserInput>({
    mutationFn: ({ id, ...body }) => patch<UserOut>(`/users/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userKeys.list });
    },
  });
}

/** Re-issue a one-time password-setup link for a user (ADMIN). */
export function useUserSetupLink(): UseMutationResult<SetupLinkResult, ApiError, number> {
  const { post } = useApi();
  return useMutation<SetupLinkResult, ApiError, number>({
    mutationFn: (id) => post<SetupLinkResult>(`/users/${id}/setup-link`),
  });
}
