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
 */
export function MockAuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(MOCK_USER);

  const signIn = useCallback(async (email: string, _password: string) => {
    // No real credential check — mock accepts anything and signs in as admin.
    void _password;
    setUser({ ...MOCK_USER, email: email || MOCK_USER.email });
  }, []);

  const signOut = useCallback(async () => {
    setUser(null);
  }, []);

  const getToken = useCallback(async () => {
    return user ? MOCK_TOKEN : null;
  }, [user]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading: false, signIn, signOut, getToken }),
    [user, signIn, signOut, getToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * Default provider for the app. Swap this for `FirebaseAuthProvider` once
 * Firebase credentials exist — the exported context/hook shape does not change.
 *
 * TODO(auth): implement FirebaseAuthProvider with the SAME AuthContextValue:
 *   - subscribe to firebase/auth `onIdTokenChanged` -> setUser(mapped claims)
 *   - signIn  -> signInWithEmailAndPassword(auth, email, password)
 *   - signOut -> firebaseSignOut(auth)
 *   - getToken -> auth.currentUser?.getIdToken() ?? null
 *   - map the OpsHub `role` from a custom claim (set via Firebase Admin SDK).
 * Then export `AuthProvider = FirebaseAuthProvider` here; consumers are unchanged.
 */
export const AuthProvider = MockAuthProvider;

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an <AuthProvider>');
  }
  return ctx;
}
