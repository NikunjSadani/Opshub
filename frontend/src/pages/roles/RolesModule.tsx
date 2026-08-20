import { useMemo, useState } from 'react';
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import { useAssignableModules, useDeleteRole, useRoles, type Role } from '../../api/roles';
import { RoleEditorModal, type EditorMode } from './RoleEditorModal';
import { buildTitleByKey, errorMessage, RoleGrantCell } from './rolesFormat';

interface EditorState {
  mode: EditorMode;
  role: Role | null;
}

/**
 * Roles admin (RBAC v2). Lists roles with a compact grant summary + how many
 * users hold each, and lets an admin create / edit / clone / delete them.
 * Built-in (system) roles are read-only — they show a "Built-in" badge and
 * offer only Clone. Server-side RBAC is the real gate; the route is ADMIN-only.
 */
export function RolesModule() {
  const toast = useToast();
  const query = useRoles();
  const modulesQuery = useAssignableModules();
  const deleteRole = useDeleteRole();

  const [editor, setEditor] = useState<EditorState | null>(null);
  const [deleting, setDeleting] = useState<Role | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const titleByKey = useMemo(
    () => buildTitleByKey(modulesQuery.data ?? []),
    [modulesQuery.data],
  );

  function openDelete(role: Role) {
    setDeleteError(null);
    setDeleting(role);
  }

  function confirmDelete() {
    if (!deleting) return;
    deleteRole.mutate(deleting.id, {
      onSuccess: () => {
        toast.success(`Deleted ${deleting.name}.`);
        setDeleting(null);
      },
      onError: (err) => {
        // 409 = system role or still-held; surface the server's honest reason
        // inline in the dialog AND as a toast, and keep the dialog open.
        setDeleteError(errorMessage(err));
        toast.error(errorMessage(err));
      },
    });
  }

  return (
    <div>
      <PageHeader
        title="Roles"
        subtitle="Define what each role can see and do across modules and the platform."
        actions={
          <Button size="sm" onClick={() => setEditor({ mode: 'create', role: null })}>
            New role
          </Button>
        }
      />

      {query.isPending ? (
        <Loading label="Loading roles…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <StatePanel title="No roles yet">Create a role to get started.</StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Name</Th>
              <Th>Description</Th>
              <Th>Grants</Th>
              <Th className="text-right">Users</Th>
              <Th>{''}</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((r) => (
              <Tr key={r.id}>
                <Td className="font-medium text-slate-900">
                  <span className="inline-flex items-center gap-2">
                    {r.name}
                    {r.is_system && <Badge tone="blue">Built-in</Badge>}
                  </span>
                </Td>
                <Td className="text-slate-600">
                  {r.description ? r.description : <span className="text-slate-400">—</span>}
                </Td>
                <Td>
                  <RoleGrantCell role={r} titleByKey={titleByKey} />
                </Td>
                <Td className="text-right tabular-nums text-slate-700">{r.user_count}</Td>
                <Td className="text-right">
                  <div className="flex justify-end gap-1.5">
                    {!r.is_system && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => setEditor({ mode: 'edit', role: r })}
                      >
                        Edit
                      </Button>
                    )}
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setEditor({ mode: 'clone', role: r })}
                    >
                      Clone
                    </Button>
                    {!r.is_system && (
                      <Button variant="ghost" size="sm" onClick={() => openDelete(r)}>
                        Delete
                      </Button>
                    )}
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {/* Conditionally MOUNTED so each open is a fresh editor: no stale name /
          levels from a previous invocation can flash before the seed effect runs. */}
      {editor !== null && (
        <RoleEditorModal
          open
          mode={editor.mode}
          role={editor.role}
          onClose={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Delete role"
        danger
        confirmLabel="Delete"
        loading={deleteRole.isPending}
        onCancel={() => {
          if (deleteRole.isPending) return;
          setDeleting(null);
        }}
        onConfirm={confirmDelete}
        message={
          <div className="space-y-2">
            <p>
              Delete <span className="font-medium text-slate-900">{deleting?.name}</span>? This
              cannot be undone.
            </p>
            {deleting && deleting.user_count > 0 && (
              <p className="text-amber-600">
                {deleting.user_count} user{deleting.user_count === 1 ? '' : 's'} currently hold this
                role — reassign them first.
              </p>
            )}
            {deleteError && (
              <p role="alert" className="text-rose-600">
                {deleteError}
              </p>
            )}
          </div>
        }
      />
    </div>
  );
}
