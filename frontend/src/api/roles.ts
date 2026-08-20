import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the RBAC v2 Roles admin.
 * Mirrors the backend Roles routes (RoleOut). All paths are relative to
 * `/api/v1` (added by `useApi()`), bearer auto-attached.
 *
 * A Role bundles per-module access LEVELS (View < Operate < Manage) plus a set
 * of platform-wide permission keys (e.g. `iam`, `settings`). A module absent
 * from `module_levels` grants no access to that module.
 */

/** Per-module access level, ordered View < Operate < Manage. */
export type ModuleLevel = 'VIEW' | 'OPERATE' | 'MANAGE';

/** A role as served by `GET /roles`. */
export interface Role {
  id: number;
  name: string;
  description: string;
  /** Built-in roles (e.g. Administrator) are protected: not editable/deletable. */
  is_system: boolean;
  /** module key -> granted level. A missing key means NO access to that module. */
  module_levels: Record<string, ModuleLevel>;
  /** Platform-wide permission keys granted (e.g. "iam", "settings"). */
  platform: string[];
  /** How many users currently hold this role (blocks delete while > 0). */
  user_count: number;
}

/** A module a role may be granted a level in (`GET /roles/assignable-modules`). */
export interface ModuleOption {
  key: string;
  title: string;
}

/** Body for `POST /roles` and `PATCH /roles/{id}`. */
export interface RoleInput {
  name: string;
  description?: string;
  module_levels: Record<string, ModuleLevel>;
  platform: string[];
}

// --------------------------------------------------------------- query keys
export const roleKeys = {
  list: ['roles', 'list'] as const,
  assignableModules: ['roles', 'assignable-modules'] as const,
};

// ------------------------------------------------------------------ queries

/** All roles (ADMIN-only server-side). */
export function useRoles(): UseQueryResult<Role[], Error> {
  const { get } = useApi();
  return useQuery<Role[], Error>({
    queryKey: roleKeys.list,
    queryFn: ({ signal }) => get<Role[]>('/roles', signal),
  });
}

/** The modules a role may be granted a level in. */
export function useAssignableModules(): UseQueryResult<ModuleOption[], Error> {
  const { get } = useApi();
  return useQuery<ModuleOption[], Error>({
    queryKey: roleKeys.assignableModules,
    queryFn: ({ signal }) => get<ModuleOption[]>('/roles/assignable-modules', signal),
    staleTime: 5 * 60 * 1000,
  });
}

// ---------------------------------------------------------------- mutations

/** Create a role (ADMIN). 400 on a bad level/unknown module, 409 on duplicate name. */
export function useCreateRole(): UseMutationResult<Role, ApiError, RoleInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Role, ApiError, RoleInput>({
    mutationFn: (body) => post<Role>('/roles', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: roleKeys.list });
    },
  });
}

/** Update a role (ADMIN). 409 when the target is a protected system role. */
export function useUpdateRole(): UseMutationResult<Role, ApiError, { id: number } & RoleInput> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<Role, ApiError, { id: number } & RoleInput>({
    mutationFn: ({ id, ...body }) => patch<Role>(`/roles/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: roleKeys.list });
    },
  });
}

/** Delete a role (ADMIN). 409 when it is a system role OR any user still holds it. */
export function useDeleteRole(): UseMutationResult<void, ApiError, number> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<void, ApiError, number>({
    mutationFn: (id) => del<void>(`/roles/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: roleKeys.list });
    },
  });
}
