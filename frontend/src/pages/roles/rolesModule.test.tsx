import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiError } from '../../api/client';
import { ToastProvider } from '../../ui';
import { RolesModule } from './RolesModule';

/**
 * Roles admin tests. The api/roles hooks are mocked directly (no network) so we
 * exercise the screen's behaviour: read-only system role, the module_levels the
 * editor builds (None omits the module), delete wiring, and an honest 409.
 */

const h = vi.hoisted(() => ({
  createMutate: vi.fn(),
  updateMutate: vi.fn(),
  deleteMutate: vi.fn(),
}));

const MODULES = [
  { key: 'document_automation', title: 'Delivery Challan' },
  { key: 'projects', title: 'Projects' },
];

const ROLES = [
  {
    id: 1,
    name: 'Administrator',
    description: 'Full access',
    is_system: true,
    module_levels: { document_automation: 'MANAGE', projects: 'MANAGE' },
    platform: ['iam', 'settings'],
    user_count: 3,
  },
  {
    id: 2,
    name: 'Operations',
    description: 'Day-to-day ops',
    is_system: false,
    module_levels: { document_automation: 'OPERATE' },
    platform: [],
    user_count: 0,
  },
  {
    id: 3,
    name: 'Finance',
    description: 'Money stuff',
    is_system: false,
    module_levels: { expense_invoice: 'VIEW' },
    platform: [],
    user_count: 2,
  },
];

vi.mock('../../api/roles', () => ({
  useRoles: () => ({ data: ROLES, isPending: false, isError: false }),
  useAssignableModules: () => ({ data: MODULES, isPending: false, isError: false }),
  useCreateRole: () => ({
    mutate: h.createMutate,
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
  useUpdateRole: () => ({
    mutate: h.updateMutate,
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
  useDeleteRole: () => ({
    mutate: h.deleteMutate,
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
}));

function renderModule(node: ReactNode) {
  return render(<ToastProvider>{node}</ToastProvider>);
}

beforeEach(() => {
  h.createMutate.mockReset();
  h.updateMutate.mockReset();
  h.deleteMutate.mockReset();
});

describe('RolesModule', () => {
  it('renders roles and keeps the built-in Administrator read-only', () => {
    renderModule(<RolesModule />);

    expect(screen.getByText('Administrator')).toBeInTheDocument();
    expect(screen.getByText('Operations')).toBeInTheDocument();
    // The system role is flagged and offers only Clone (no Edit/Delete).
    expect(screen.getByText('Built-in')).toBeInTheDocument();
    const adminRow = screen.getByText('Administrator').closest('tr') as HTMLElement;
    expect(within(adminRow).queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(within(adminRow).queryByRole('button', { name: 'Delete' })).toBeNull();
    expect(within(adminRow).getByRole('button', { name: 'Clone' })).toBeInTheDocument();
    // Editable roles keep Edit + Delete.
    expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    // Compact grant summary resolves module keys to titles + level.
    expect(screen.getByText('Delivery Challan: Operate')).toBeInTheDocument();
  });

  it('builds module_levels from the editor, omitting modules left at None', async () => {
    renderModule(<RolesModule />);

    fireEvent.click(screen.getByRole('button', { name: 'New role' }));
    fireEvent.change(await screen.findByLabelText(/name/i), {
      target: { value: 'Ops Lead' },
    });

    // Grant Delivery Challan = Operate; leave Projects at None.
    const challan = screen.getByRole('radiogroup', { name: 'Delivery Challan' });
    fireEvent.click(within(challan).getByRole('radio', { name: 'Operate' }));

    fireEvent.click(screen.getByRole('button', { name: 'Create role' }));

    await waitFor(() => expect(h.createMutate).toHaveBeenCalledTimes(1));
    expect(h.createMutate.mock.calls[0][0]).toEqual({
      name: 'Ops Lead',
      description: '',
      module_levels: { document_automation: 'OPERATE' },
      platform: [],
    });
  });

  it('deletes a role via the confirm dialog', async () => {
    renderModule(<RolesModule />);

    // Operations is the first editable (non-system) row.
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(h.deleteMutate).toHaveBeenCalledTimes(1);
    expect(h.deleteMutate.mock.calls[0][0]).toBe(2);
  });

  it('surfaces a 409 "role in use" message and keeps the dialog open', async () => {
    h.deleteMutate.mockImplementation(
      (_id: number, opts: { onError?: (e: unknown) => void }) => {
        opts.onError?.(
          new ApiError(409, '2 user(s) still hold this role — reassign them first.'),
        );
      },
    );

    renderModule(<RolesModule />);

    // Finance (id 3) is the second editable row and is still held by 2 users.
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[1]);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    // The server's honest reason is surfaced inline in the still-open dialog
    // (the identical toast copy is scoped out by querying within the dialog).
    const openDialog = await screen.findByRole('dialog');
    expect(
      within(openDialog).getByText(/2 user\(s\) still hold this role/i),
    ).toBeInTheDocument();
  });
});
