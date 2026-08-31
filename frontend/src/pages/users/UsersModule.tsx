import { useState } from 'react';
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
import { useUsers, type UserOut } from '../../api/users';
import { InviteUserModal } from './InviteUserModal';
import { EditUserModal } from './EditUserModal';
import { formatDateTime, roleLabel } from './usersFormat';

/**
 * User Management (ADMIN, RBAC v2). Lists users and their assigned role, invites
 * new users (with a one-time setup link), and edits role / active status. Roles
 * themselves are managed on the separate Roles screen. Server-side RBAC is the
 * real gate; this screen is ADMIN-guarded at the route.
 */
export function UsersModule() {
  const query = useUsers();

  const [inviteOpen, setInviteOpen] = useState(false);
  const [editing, setEditing] = useState<UserOut | null>(null);

  return (
    <div>
      <PageHeader
        title="Users"
        subtitle="Manage who can sign in and which role they hold."
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
              <Th>Last active</Th>
              <Th>{''}</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((u) => (
              <Tr key={u.id}>
                <Td className="font-medium text-slate-900">{u.email}</Td>
                <Td className="text-slate-900">{u.name}</Td>
                <Td>
                  {u.role_name ? (
                    <Badge tone="blue">{u.role_name}</Badge>
                  ) : (
                    <span className="text-xs text-slate-400">{roleLabel(u.role_name)}</span>
                  )}
                </Td>
                <Td>
                  <Badge tone={u.active ? 'green' : 'slate'}>
                    {u.active ? 'Active' : 'Disabled'}
                  </Badge>
                  {!u.is_provisioned && (
                    <span className="ml-1.5 text-[11px] text-amber-600">setup pending</span>
                  )}
                </Td>
                <Td className="whitespace-nowrap text-slate-600">
                  <div className="leading-tight">
                    <div>{formatDateTime(u.last_seen_at)}</div>
                    <div className="text-[11px] text-slate-400">
                      signed in {formatDateTime(u.last_login_at)}
                    </div>
                  </div>
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
