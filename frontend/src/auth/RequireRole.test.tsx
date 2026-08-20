import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MockAuthProvider } from './AuthProvider';
import { RequirePlatform } from './RequireRole';

/**
 * Stub `GET /me` so the permissions context resolves to a set that either
 * holds `iam` (dev-admin) or holds nothing (dev-viewer), keyed off the
 * `X-Dev-Uid` header the mock provider sends. Any other call fails loudly.
 */
function stubMe() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/me')) {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        const uid = headers['X-Dev-Uid'] ?? 'dev-admin';
        const isAdmin = uid === 'dev-admin';
        return new Response(
          JSON.stringify({
            id: 1,
            email: 'test@example.com',
            name: 'Test User',
            role_id: 1,
            role_name: isAdmin ? 'Administrator' : 'Viewer',
            is_administrator: isAdmin,
            module_levels: {},
            platform: isAdmin ? ['iam', 'settings'] : [],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
}

/** Render RequirePlatform under the mock provider acting as a given seeded user. */
function renderAs(uid: string, perm: string, fallback?: ReactNode) {
  stubMe();
  return render(
    <MockAuthProvider initialUid={uid}>
      <RequirePlatform perm={perm} fallback={fallback}>
        <p>Secret admin panel</p>
      </RequirePlatform>
    </MockAuthProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('RequirePlatform', () => {
  it('renders children when the user holds the permission', async () => {
    renderAs('dev-admin', 'iam');
    expect(await screen.findByText('Secret admin panel')).toBeInTheDocument();
    expect(screen.queryByText('Not authorized')).not.toBeInTheDocument();
  });

  it('shows the not-authorized panel when the permission is missing', async () => {
    renderAs('dev-viewer', 'iam');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Not authorized');
    // The panel names the permission that would grant access.
    expect(alert).toHaveTextContent('iam');
    expect(screen.queryByText('Secret admin panel')).not.toBeInTheDocument();
  });

  it('renders the custom fallback (nothing) on a permission miss when provided', async () => {
    renderAs('dev-viewer', 'iam', null);
    // Once /me resolves, the loading placeholder is gone and — with a null
    // fallback — neither the children nor the not-authorized panel render.
    await waitFor(() =>
      expect(screen.queryByText('Loading…')).not.toBeInTheDocument(),
    );
    expect(screen.queryByText('Secret admin panel')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
