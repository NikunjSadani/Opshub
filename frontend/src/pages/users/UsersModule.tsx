import { useMemo, useState } from 'react';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
} from '../../ui';
import { useAssignableModules, useUsers, type UserOut } from '../../api/users';
import { InviteUserModal } from './InviteUserModal';
import { EditUserModal } from './EditUserModal';
import { ROLE_LABEL } from './usersFormat';

/** Chips for a user's granted modules, showing human titles where known. */
function ModuleChips({ keys, titles }: { keys: string[]; titles: Map<string, string> }) {
  if (keys.length === 0) return <span className="text-xs text-slate-400">None</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {keys.map((k) => (
        <span
          key={k}
          className="inline-flex items-center rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600"
        >
          {titles.get(k) ?? k}
        </span>
      ))}
    </div>
  );
}

/**
 * User Management (ADMIN). Lists users and their access, invites new users (with
 * a one-time setup link), and edits role / active / module grants. Server-side
 * RBAC is the real gate; this screen is ADMIN-guarded at the route.
 */
export function UsersModule() {
  const query = useUsers();
  // Assignable modules give us key -> title for the granted-module chips. Admins
  // are the only ones who reach this screen, so the list is always available.
  const modulesQuery = useAssignableModules();

  const [inviteOpen, setInviteOpen] = useState(false);
  const [editing, setEditing] = useState<UserOut | null>(null);

  const titleByKey = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of modulesQuery.data ?? []) map.set(m.key, m.title);
    return map;
  }, [modulesQuery.data]);

  return (
    <div>
      <PageHeader
        title="Users"
        subtitle="Manage who can sign in, their role, and which modules they can access."
        actions={
          <Button size="sm" onClick={() => setInviteOpen(true)}>
            Invite user
          </Button>
        }
      />

      {query.isPending ? (
        <Loading label="Loading users…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <StatePanel title="No users yet">Invite a user to get started.</StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Email</Th>
              <Th>Name</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th>Modules</Th>
              <Th>{''}</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((u) => (
              <Tr key={u.id}>
                <Td className="font-medium text-slate-900">{u.email}</Td>
                <Td className="text-slate-900">{u.name}</Td>
                <Td>
                  <Badge tone={u.role === 'ADMIN' ? 'blue' : 'slate'}>{ROLE_LABEL[u.role]}</Badge>
                </Td>
                <Td>
                  <Badge tone={u.active ? 'green' : 'slate'}>
                    {u.active ? 'Active' : 'Disabled'}
                  </Badge>
                  {!u.is_provisioned && (
                    <span className="ml-1.5 text-[11px] text-amber-600">setup pending</span>
                  )}
                </Td>
                <Td>
                  <ModuleChips keys={u.module_keys} titles={titleByKey} />
                </Td>
                <Td className="text-right">
                  <Button variant="secondary" size="sm" onClick={() => setEditing(u)}>
                    Edit
                  </Button>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {/* Conditionally MOUNTED (not just `open`-toggled): each open is a fresh mount,
          so a previous invocation's state — including another user's one-time setup
          link — can never flash into a newly-opened dialog before an effect resets it. */}
      {inviteOpen && <InviteUserModal open onClose={() => setInviteOpen(false)} />}
      {editing !== null && (
        <EditUserModal open user={editing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
