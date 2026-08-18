import { useEffect, useState } from 'react';
import { Button, Modal, SelectField, TextField, useToast } from '../../ui';
import { ROLES } from '../../auth/AuthProvider';
import {
  useUpdateUser,
  useUserSetupLink,
  type Role,
  type UpdateUserInput,
  type UserOut,
} from '../../api/users';
import { ModuleCheckboxes } from './ModuleCheckboxes';
import { SetupLinkPanel } from './SetupLinkPanel';
import { errorMessage, ROLE_LABEL } from './usersFormat';

/** Compare two module-key sets order-independently. */
function sameKeys(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every((k) => setB.has(k));
}

/**
 * Edit-user modal (ADMIN). Change name / role / active / module grants, and
 * re-issue a one-time setup link. Only changed fields are sent. Server guards
 * (last-admin, self-lockout) surface as readable toast messages.
 */
export function EditUserModal({
  open,
  user,
  onClose,
}: {
  open: boolean;
  user: UserOut | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const updateUser = useUpdateUser();
  const setupLink = useUserSetupLink();

  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('OPERATIONS');
  const [active, setActive] = useState(true);
  const [moduleKeys, setModuleKeys] = useState<string[]>([]);
  const [reissued, setReissued] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    if (!open || !user) return;
    setName(user.name);
    setRole(user.role);
    setActive(user.active);
    setModuleKeys(user.module_keys);
    setReissued(undefined);
    updateUser.reset();
    setupLink.reset();
    // Re-seed only when the target user / open state changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user]);

  if (!user) return null;

  const trimmedName = name.trim();
  const busy = updateUser.isPending;

  // Build the partial patch of only the fields that actually changed.
  const patch: UpdateUserInput = {};
  if (trimmedName && trimmedName !== user.name) patch.name = trimmedName;
  if (role !== user.role) patch.role = role;
  if (active !== user.active) patch.active = active;
  if (!sameKeys(moduleKeys, user.module_keys)) patch.module_keys = moduleKeys;

  const hasChanges = Object.keys(patch).length > 0;
  const canSubmit = hasChanges && trimmedName.length > 0 && !busy;

  function toggleModule(key: string, checked: boolean) {
    setModuleKeys((prev) =>
      checked ? [...new Set([...prev, key])] : prev.filter((k) => k !== key),
    );
  }

  function close() {
    if (busy) return;
    onClose();
  }

  function submit() {
    if (!canSubmit || !user) return;
    updateUser.mutate(
      { id: user.id, ...patch },
      {
        onSuccess: (updated) => {
          toast.success(`Saved ${updated.email}.`);
          onClose();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  function toggleActive() {
    setActive((v) => !v);
  }

  function reissue() {
    if (!user) return;
    setupLink.mutate(user.id, {
      onSuccess: (res) => {
        setReissued(res.setup_link);
        // Don't claim success when nothing was issued (local/unprovisioned → null).
        if (res.setup_link) toast.success('Setup link re-issued.');
        else toast.info('No setup link available yet — auth isn’t configured.');
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <Modal
      open={open}
      title={`Edit ${user.email}`}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!canSubmit}>
            Save changes
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="Name"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={120}
        />
        <SelectField
          label="Role"
          required
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_LABEL[r]}
            </option>
          ))}
        </SelectField>

        <div className="flex items-center justify-between rounded-md border border-slate-200 px-3 py-2">
          <div>
            <p className="text-sm font-medium text-slate-700">Account status</p>
            <p className="text-xs text-slate-400">
              {active ? 'Active — can sign in.' : 'Disabled — sign-in blocked.'}
            </p>
          </div>
          <Button
            variant={active ? 'danger' : 'primary'}
            size="sm"
            onClick={toggleActive}
            disabled={busy}
            aria-pressed={active}
          >
            {active ? 'Disable' : 'Enable'}
          </Button>
        </div>

        <ModuleCheckboxes selected={moduleKeys} onToggle={toggleModule} disabled={busy} />

        <div className="border-t border-slate-100 pt-3">
          {reissued !== undefined ? (
            <SetupLinkPanel setupLink={reissued} />
          ) : (
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-slate-500">
                {user.is_provisioned
                  ? 'User has set a password. Re-issue a link to let them reset it.'
                  : 'User has not set a password yet.'}
              </p>
              <Button
                variant="secondary"
                size="sm"
                onClick={reissue}
                loading={setupLink.isPending}
              >
                Re-issue setup link
              </Button>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
