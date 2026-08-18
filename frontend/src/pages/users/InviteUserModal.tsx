import { useEffect, useState } from 'react';
import { Button, Modal, SelectField, TextField, useToast } from '../../ui';
import { ROLES } from '../../auth/AuthProvider';
import { useCreateUser, type CreateUserResult, type Role } from '../../api/users';
import { ModuleCheckboxes } from './ModuleCheckboxes';
import { SetupLinkPanel } from './SetupLinkPanel';
import { errorMessage, isValidEmail, ROLE_LABEL } from './usersFormat';

/**
 * Invite-user modal. Collects email / name / role / module grants, POSTs, then
 * flips to a success view showing the one-time setup link. NEVER collects a
 * password.
 */
export function InviteUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const createUser = useCreateUser();

  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('OPERATIONS');
  const [moduleKeys, setModuleKeys] = useState<string[]>([]);
  const [result, setResult] = useState<CreateUserResult | null>(null);

  // Reset fields + mutation state whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    setEmail('');
    setName('');
    setRole('OPERATIONS');
    setModuleKeys([]);
    setResult(null);
    createUser.reset();
    // Only re-run on open transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const trimmedName = name.trim();
  const emailValid = isValidEmail(email);
  const emailError = email.length > 0 && !emailValid ? 'Enter a valid email address.' : undefined;
  const canSubmit = emailValid && trimmedName.length > 0 && !createUser.isPending;

  function toggleModule(key: string, checked: boolean) {
    setModuleKeys((prev) =>
      checked ? [...new Set([...prev, key])] : prev.filter((k) => k !== key),
    );
  }

  function close() {
    if (createUser.isPending) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    createUser.mutate(
      { email: email.trim(), name: trimmedName, role, module_keys: moduleKeys },
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
        footer={
          <Button onClick={onClose}>Done</Button>
        }
      >
        <div className="space-y-3">
          <p className="text-sm text-slate-600">
            <span className="font-medium text-slate-900">{result.user.name}</span> (
            {result.user.email}) was created as {ROLE_LABEL[result.user.role]}.
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
        <ModuleCheckboxes selected={moduleKeys} onToggle={toggleModule} />
      </div>
    </Modal>
  );
}
