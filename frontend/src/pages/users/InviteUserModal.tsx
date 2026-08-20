import { useEffect, useState } from 'react';
import { Button, Modal, SelectField, TextField, useToast } from '../../ui';
import { useAssignableRoles, useCreateUser, type CreateUserResult } from '../../api/users';
import { SetupLinkPanel } from './SetupLinkPanel';
import { errorMessage, isValidEmail, roleLabel } from './usersFormat';

/**
 * Invite-user modal (RBAC v2). Collects email / name / role, POSTs, then flips
 * to a success view showing the one-time setup link. The role is chosen from the
 * list of existing roles (managed on the Roles screen). NEVER collects a password.
 */
export function InviteUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const createUser = useCreateUser();
  const roles = useAssignableRoles();

  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [roleId, setRoleId] = useState<number | null>(null);
  const [result, setResult] = useState<CreateUserResult | null>(null);

  // Reset fields + mutation state whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    setEmail('');
    setName('');
    setRoleId(null);
    setResult(null);
    createUser.reset();
    // Only re-run on open transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Default to the first available role once the list loads (nothing chosen yet).
  const roleList = roles.data;
  useEffect(() => {
    if (roleId === null && roleList && roleList.length > 0) {
      setRoleId(roleList[0].id);
    }
  }, [roleId, roleList]);

  const trimmedName = name.trim();
  const emailValid = isValidEmail(email);
  const emailError = email.length > 0 && !emailValid ? 'Enter a valid email address.' : undefined;
  const canSubmit =
    emailValid && trimmedName.length > 0 && roleId !== null && !createUser.isPending;

  function close() {
    if (createUser.isPending) return;
    onClose();
  }

  function submit() {
    if (!canSubmit || roleId === null) return;
    createUser.mutate(
      { email: email.trim(), name: trimmedName, role_id: roleId },
      {
        onSuccess: (res) => {
          setResult(res);
          toast.success(`Invited ${res.user.email}.`);
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  // --- success view: show the setup link, then a single "Done" action.
  if (result) {
    return (
      <Modal
        open={open}
        title="User invited"
        onClose={onClose}
        footer={<Button onClick={onClose}>Done</Button>}
      >
        <div className="space-y-3">
          <p className="text-sm text-slate-600">
            <span className="font-medium text-slate-900">{result.user.name}</span> (
            {result.user.email}) was created as {roleLabel(result.user.role_name)}.
          </p>
          <SetupLinkPanel setupLink={result.setup_link} />
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      title="Invite user"
      onClose={close}
      busy={createUser.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={createUser.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={createUser.isPending} disabled={!canSubmit}>
            Send invite
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="Email"
          type="email"
          required
          value={email}
          error={emailError}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="e.g. jane@gifsy.in"
          maxLength={200}
        />
        <TextField
          label="Name"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Jane Doe"
          maxLength={120}
        />
        <RoleSelectField
          value={roleId}
          onChange={setRoleId}
          roles={roles.data}
          loading={roles.isPending}
          error={roles.isError}
        />
      </div>
    </Modal>
  );
}

/**
 * Role picker shared by the invite + edit forms. Renders one option per existing
 * role (value = role id, label = role name), with honest disabled placeholders
 * while the list is loading, errored, or empty.
 */
export function RoleSelectField({
  value,
  onChange,
  roles,
  loading,
  error,
  disabled = false,
}: {
  value: number | null;
  onChange: (id: number) => void;
  roles: { id: number; name: string }[] | undefined;
  loading: boolean;
  error: boolean;
  disabled?: boolean;
}) {
  const hasRoles = !!roles && roles.length > 0;
  const placeholder = loading
    ? 'Loading roles…'
    : error
      ? 'Failed to load roles'
      : hasRoles
        ? 'Select a role…'
        : 'No roles available';

  return (
    <SelectField
      label="Role"
      required
      value={value === null ? '' : String(value)}
      disabled={disabled || !hasRoles}
      onChange={(e) => {
        const next = Number.parseInt(e.target.value, 10);
        if (Number.isFinite(next)) onChange(next);
      }}
    >
      <option value="" disabled>
        {placeholder}
      </option>
      {roles?.map((r) => (
        <option key={r.id} value={String(r.id)}>
          {r.name}
        </option>
      ))}
    </SelectField>
  );
}
