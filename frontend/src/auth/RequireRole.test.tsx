import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MockAuthProvider, type Role } from './AuthProvider';
import { RequireRole } from './RequireRole';

/**
 * Render RequireRole under the mock provider pinned to a given role. The
 * mock-only `initialRole` prop drives the same role state the dev switcher
 * flips at runtime, so this exercises the real guard path.
 */
function renderAsRole(role: Role, allow: Role[]) {
  return render(
    <MockAuthProvider initialRole={role}>
      <RequireRole allow={allow}>
        <p>Secret admin panel</p>
      </RequireRole>
    </MockAuthProvider>,
  );
}

describe('RequireRole', () => {
  it('renders children when the role is allowed', () => {
    renderAsRole('ADMIN', ['ADMIN', 'OPERATIONS']);
    expect(screen.getByText('Secret admin panel')).toBeInTheDocument();
    expect(screen.queryByText('Not authorized')).not.toBeInTheDocument();
  });

  it('shows the not-authorized panel when the role is disallowed', () => {
    renderAsRole('FINANCE', ['ADMIN']);
    expect(screen.queryByText('Secret admin panel')).not.toBeInTheDocument();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Not authorized');
    // The panel names the roles that would have access.
    expect(alert).toHaveTextContent('ADMIN');
  });

  it('renders the custom fallback (nothing) on a role miss when provided', () => {
    render(
      <MockAuthProvider initialRole="MIS">
        <RequireRole allow={['ADMIN']} fallback={null}>
          <p>Secret admin panel</p>
        </RequireRole>
      </MockAuthProvider>,
    );
    expect(screen.queryByText('Secret admin panel')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
