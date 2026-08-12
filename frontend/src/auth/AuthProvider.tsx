import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

/**
 * OpsHub roles (see docs/DESIGN.md §3). A user's `role` governs actions;
 * a separate per-user module-access list (served by the backend) governs
 * which modules are visible. The shell only needs the identity + a token
 * getter here — authorization is enforced server-side.
 */
export type Role = 'ADMIN' | 'MIS' | 'OPERATIONS' | 'FINANCE';

/** All roles, in a stable display order. Handy for guards and the dev switcher. */
export const ROLES: readonly Role[] = ['ADMIN', 'MIS', 'OPERATIONS', 'FINANCE'];

export interface AuthUser {
  uid: string;
  email: string;
  name: string;
  role: Role;
}

/**
 * The single auth contract the whole app depends on. The mock implementation
 * below satisfies it today; a real `FirebaseAuthProvider` will implement the
 * exact same shape later (see the TODO) so nothing downstream changes.
 */
export interface AuthContextValue {
  user: AuthUser | null;
  /** True while the initial auth state is resolving (always false for mock). */
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  /**
   * Returns a bearer token for API calls, or null when signed out.
   * Real impl returns the Firebase ID token (auto-refreshed by the SDK).
   */
  getToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// ---------------------------------------------------------------------------
// Mock implementation — lets the shell build & run with no Firebase creds.
// ---------------------------------------------------------------------------

/**
 * Mock-only dev controls, kept DELIBERATELY OUT of `AuthContextValue`.
 *
 * The real `FirebaseAuthProvider` must implement `AuthContextValue` exactly,
 * and Firebase derives `role` from a signed custom claim — there is no
 * client-side role setter in the real world. So the ability to switch roles
 * lives in a SEPARATE context that only `MockAuthProvider` supplies. Any UI
 * that wants it calls `useMockDevControls()`, which returns `null` under a
 * real provider (and the UI then renders nothing).
 */
export interface MockDevControls {
  role: Role;
  setRole: (role: Role) => void;
  roles: readonly Role[];
}

const MockDevContext = createContext<MockDevControls | null>(null);

/**
 * Returns the mock role-switching controls, or `null` when the active provider
 * is not the mock (e.g. the real Firebase provider). Callers must handle null.
 */
export function useMockDevControls(): MockDevControls | null {
  return useContext(MockDevContext);
}

const MOCK_USER: AuthUser = {
  uid: 'mock-admin-uid',
  email: 'ops.admin@gifsy.in',
  name: 'Ops Admin',
  role: 'ADMIN',
};

const MOCK_TOKEN = 'mock-id-token.not-a-real-jwt';

/**
 * MockAuthProvider — starts signed-in as an ADMIN so the shell is immediately
 * usable, and exposes working signIn/signOut for the /login placeholder page.
 *
 * Identity and role are held in SEPARATE state so a dev tool can swap the role
 * at runtime (via {@link useMockDevControls}) without disturbing the rest of
 * the mock identity, and so the role survives a mock sign-in. The public
 * `AuthContextValue` is unchanged — role switching is a mock-only side channel.
 *
 * `initialRole` is a mock-only convenience (handy in tests); it is NOT part of
 * the auth contract the real provider implements.
 */
export function MockAuthProvider({
  children,
  initialRole = MOCK_USER.role,
}: {
  children: ReactNode;
  initialRole?: Role;
}) {
  const [account, setAccount] = useState<AuthUser | null>(MOCK_USER);
  const [role, setRole] = useState<Role>(initialRole);

  // The exposed user always reflects the currently-selected mock role.
  const user = useMemo<AuthUser | null>(
    () => (account ? { ...account, role } : null),
    [account, role],
  );

  const signIn = useCallback(async (email: string, _password: string) => {
    // No real credential check — mock accepts anything and keeps the current role.
    void _password;
    setAccount({ ...MOCK_USER, email: email || MOCK_USER.email });
  }, []);

  const signOut = useCallback(async () => {
    setAccount(null);
  }, []);

  const getToken = useCallback(async () => {
    return account ? MOCK_TOKEN : null;
  }, [account]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading: false, signIn, signOut, getToken }),
    [user, signIn, signOut, getToken],
  );

  const devControls = useMemo<MockDevControls>(
    () => ({ role, setRole, roles: ROLES }),
    [role],
  );

  return (
    <AuthContext.Provider value={value}>
      <MockDevContext.Provider value={devControls}>
        {children}
      </MockDevContext.Provider>
    </AuthContext.Provider>
  );
}

/** Fail-closed screen shown when a PRODUCTION build has no real auth wired. */
function AuthNotConfigured() {
  return (
    <div className="grid h-screen place-items-center bg-slate-50 p-6 text-center">
      <div className="max-w-md">
        <h1 className="text-lg font-semibold text-slate-900">Authentication not configured</h1>
        <p className="mt-2 text-sm text-slate-600">
          This build has no authentication provider wired up. Mock auth (which
          signs everyone in as an admin) is disabled outside development. Wire up
          FirebaseAuthProvider before deploying — see the TODO in AuthProvider.tsx.
        </p>
      </div>
    </div>
  );
}

/**
 * Default provider for the app. In development it uses the mock (auto-admin)
 * provider; in a PRODUCTION build it FAILS CLOSED — open mock auth must never
 * ship. Until the real provider is wired, a prod build renders a hard block
 * instead of silently authenticating every visitor as an admin.
 *
 * TODO(auth): implement FirebaseAuthProvider with the SAME AuthContextValue:
 *   - subscribe to firebase/auth `onIdTokenChanged` -> setUser(mapped claims)
 *   - signIn  -> signInWithEmailAndPassword(auth, email, password)
 *   - signOut -> firebaseSignOut(auth)
 *   - getToken -> auth.currentUser?.getIdToken() ?? null
 *   - map the OpsHub `role` from a custom claim (set via Firebase Admin SDK).
 * Then return `<FirebaseAuthProvider>` for the production branch below.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  if (import.meta.env.PROD) {
    return <AuthNotConfigured />;
  }
  return <MockAuthProvider>{children}</MockAuthProvider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an <AuthProvider>');
  }
  return ctx;
}
