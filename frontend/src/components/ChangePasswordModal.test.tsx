import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ToastProvider } from '../ui';
import { ChangePasswordModal } from './ChangePasswordModal';

// Mock the auth context so the test drives `changePassword` directly (no Firebase).
const changePassword = vi.fn((_current: string, _next: string): Promise<void> => Promise.resolve());
vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ changePassword }),
}));

function renderModal(onClose = vi.fn()) {
  render(
    <ToastProvider>
      <ChangePasswordModal open onClose={onClose} />
    </ToastProvider>,
  );
  return { onClose };
}

function setField(dialog: HTMLElement, label: RegExp, value: string) {
  fireEvent.change(within(dialog).getByLabelText(label), { target: { value } });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('ChangePasswordModal', () => {
  it('blocks submit when the new password is too short', () => {
    renderModal();
    const dialog = screen.getByRole('dialog');
    setField(dialog, /current password/i, 'oldpassword');
    setField(dialog, /^new password/i, 'short'); // 5 chars < 8
    setField(dialog, /confirm new password/i, 'short');

    const submit = within(dialog).getByRole('button', { name: /change password/i });
    expect(submit).toBeDisabled();
    expect(within(dialog).getByText(/at least 8 characters/i)).toBeInTheDocument();
    fireEvent.click(submit);
    expect(changePassword).not.toHaveBeenCalled();
  });

  it('blocks submit when confirm does not match', () => {
    renderModal();
    const dialog = screen.getByRole('dialog');
    setField(dialog, /current password/i, 'oldpassword');
    setField(dialog, /^new password/i, 'newpassword1');
    setField(dialog, /confirm new password/i, 'different123');

    const submit = within(dialog).getByRole('button', { name: /change password/i });
    expect(submit).toBeDisabled();
    expect(within(dialog).getByText(/do not match/i)).toBeInTheDocument();
    fireEvent.click(submit);
    expect(changePassword).not.toHaveBeenCalled();
  });

  it('calls changePassword(current, new) and shows success on a valid submit', async () => {
    changePassword.mockResolvedValueOnce(undefined);
    const { onClose } = renderModal();
    const dialog = screen.getByRole('dialog');
    setField(dialog, /current password/i, 'oldpassword');
    setField(dialog, /^new password/i, 'newpassword1');
    setField(dialog, /confirm new password/i, 'newpassword1');

    fireEvent.click(within(dialog).getByRole('button', { name: /change password/i }));

    await waitFor(() => expect(changePassword).toHaveBeenCalledWith('oldpassword', 'newpassword1'));
    expect(await screen.findByText(/password changed/i)).toBeInTheDocument();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('surfaces the error message and stays open when changePassword rejects', async () => {
    changePassword.mockRejectedValueOnce({ code: 'auth/wrong-password' });
    const { onClose } = renderModal();
    const dialog = screen.getByRole('dialog');
    setField(dialog, /current password/i, 'wrongpass');
    setField(dialog, /^new password/i, 'newpassword1');
    setField(dialog, /confirm new password/i, 'newpassword1');

    fireEvent.click(within(dialog).getByRole('button', { name: /change password/i }));

    expect(await screen.findByText(/current password is incorrect/i)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});
