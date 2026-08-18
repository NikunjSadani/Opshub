import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';
import type { ModuleDescriptor } from '../types/modules';
import type { Role } from '../auth/AuthProvider';

/**
 * Typed contracts + React Query hooks for the User Management module.
 * Mirrors the backend Users routes (UserOut). All paths are relative to
 * `/api/v1` (added by `useApi()`), bearer auto-attached.
 *
 * NEVER models or transmits a password — accounts are provisioned via a
 * one-time setup link the admin passes to the user out-of-band.
 */

export type { Role };

/** A user as served by `GET /users`. */
export interface UserOut {
  id: number;
  email: string;
  name: string;
  role: Role;
  active: boolean;
  /** Keys of the modules this user may access. */
  module_keys: string[];
  /** True once the user has set a password (finished setup). */
  is_provisioned: boolean;
  /** ISO datetime string. */
  created_at: string;
}

/** Body for `POST /users`. */
export interface CreateUserInput {
  email: string;
  name: string;
  role: Role;
  module_keys: string[];
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
  role?: Role;
  active?: boolean;
  module_keys?: string[];
}

/** `POST /users/{id}/setup-link` response. */
export interface SetupLinkResult {
  setup_link: string | null;
}

/** A module an admin may grant to a user (filtered subset of GET /modules). */
export interface AssignableModule {
  key: string;
  title: string;
  nav_group: string;
}

// --------------------------------------------------------------- query keys
export const userKeys = {
  list: ['users', 'list'] as const,
  assignableModules: ['users', 'assignable-modules'] as const,
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
 * The modules an admin may grant. Wraps `GET /modules` and filters OUT
 * platform/system modules (`nav_group === '_system'`), not-yet-built modules
 * (`coming_soon`), and the health module — none of which are grantable.
 */
export function useAssignableModules(): UseQueryResult<AssignableModule[], Error> {
  const { get } = useApi();
  return useQuery<ModuleDescriptor[], Error, AssignableModule[]>({
    queryKey: userKeys.assignableModules,
    queryFn: ({ signal }) => get<ModuleDescriptor[]>('/modules', signal),
    staleTime: 5 * 60 * 1000,
    select: (modules) =>
      modules
        .filter((m) => m.nav_group !== '_system' && !m.coming_soon && m.key !== 'health')
        .map(({ key, title, nav_group }) => ({ key, title, nav_group })),
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

/** Update a user's name/role/active/module grants (ADMIN). Refreshes the list. */
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
