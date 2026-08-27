import { useEffect, useState } from 'react';
import { Button, Modal, TextField, useToast } from '../ui';
import { useAuth } from '../auth/AuthProvider';

const MIN_LENGTH = 8;

/** Firebase surfaces auth errors as `{ code, message }`; fall back to a generic line. */
function changePasswordError(err: unknown): string {
  const code = (err as { code?: string })?.code;
  switch (code) {
    case 'auth/wrong-password':
    case 'auth/invalid-credential':
      return 'Your current password is incorrect.';
    case 'auth/weak-password':
      return 'That new password is too weak. Choose a stronger one.';
    case 'auth/too-many-requests':
      return 'Too many attempts. Please wait a moment and try again.';
    default:
      if (err instanceof Error && err.message) return err.message;
      return 'Could not change your password. Please try again.';
  }
}

/**
 * Lets the SIGNED-IN user change their own password in place (no email, no re-login).
 * Validates locally (new ≥ 8 chars, new === confirm) before delegating to
 * `useAuth().changePassword`, which re-authenticates with the current password when
 * Firebase requires a recent login.
 */
export function ChangePasswordModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { changePassword } = useAuth();
  const toast = useToast();

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed to empty on every open so a previous attempt's values never linger.
  useEffect(() => {
    if (!open) return;
    setCurrent('');
    setNext('');
    setConfirm('');
    setSubmitting(false);
    setError(null);
  }, [open]);

  const tooShort = next.length > 0 && next.length < MIN_LENGTH;
  const mismatch = confirm.length > 0 && next !== confirm;
  const canSubmit =
    current.length > 0 &&
    next.length >= MIN_LENGTH &&
    next === confirm &&
    !submitting;

  function close() {
    if (submitting) return;
    onClose();
  }

  async function submit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await changePassword(current, next);
      toast.success('Password changed.');
      onClose();
    } catch (err) {
      setError(changePasswordError(err));
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      title="Change password"
      onClose={close}
      busy={submitting}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} loading={submitting} disabled={!canSubmit}>
            Change password
          </Button>
        </>
      }
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <TextField
          label="Current password"
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <TextField
          label="New password"
          type="password"
          autoComplete="new-password"
          required
          value={next}
          error={tooShort ? `At least ${MIN_LENGTH} characters.` : undefined}
          hint={`Use at least ${MIN_LENGTH} characters.`}
          onChange={(e) => setNext(e.target.value)}
        />
        <TextField
          label="Confirm new password"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          error={mismatch ? 'Passwords do not match.' : undefined}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {error && (
          <p role="alert" className="text-xs text-rose-600">
            {error}
          </p>
        )}
        {/* Enables Enter-to-submit inside the form without duplicating the footer button. */}
        <button type="submit" className="hidden" aria-hidden="true" tabIndex={-1} disabled={!canSubmit} />
      </form>
    </Modal>
  );
}
