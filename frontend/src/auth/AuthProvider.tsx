import {
  createContext,
  lazy,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { apiFetch } from '../api/client';

// Firebase auth is code-split so its (large) SDK loads ONLY in a production build — never in
// dev/tests, which use the mock. Lazy import keeps firebase out of the dev/test module graph.
const FirebaseAuthProvider = lazy(() =>
  import('./FirebaseAuthProvider').then((m) => ({ default: m.FirebaseAuthProvider })),
);

/**
 * Auth + permissions for the whole app (RBAC v2).
 *
 * Identity + a token come from the auth provider (mock today, Firebase later). The
 * user's ACCESS is fetched from the backend `GET /me` — the single source of truth —
 * and exposed via {@link usePermissions}: per-module levels (View/Operate/Manage) plus
 * platform permissions. The UI gates on these; the backend enforces them.
 */
export type LevelName = 'VIEW' | 'OPERATE' | 'MANAGE';

const LEVEL_RANK: Record<LevelName, number> = { VIEW: 1, OPERATE: 2, MANAGE: 3 };

export interface AuthUser {
  uid: string;
  email: string;
  name: string;
}

/** Effective permissions for the current user, as returned by `GET /me`. */
export interface Permissions {
  roleName: string | null;
  isAdministrator: boolean;
  /** module_key -> granted level; absent key = no access to that module. */
  moduleLevels: Record<string, LevelName>;
  /** platform permission keys held ("iam", "settings"). */
  platform: string[];
}

interface MeResponse {
  id: number;
  email: string;
  name: string;
  role_id: number | null;
  role_name: string | null;
  is_administrator: boolean;
  module_levels: Record<string, LevelName>;
  platform: string[];
}

export interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  getToken: () => Promise<string | null>;
  /** LOCAL dev only: the seeded user to act as (sent as `X-Dev-Uid`). Null under real auth. */
  devUid: string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// --- permissions context (populated from GET /me) ---

export interface PermissionsApi {
  loading: boolean;
  error: boolean;
  isAdministrator: boolean;
  roleName: string | null;
  moduleLevel: (moduleKey: string) => LevelName | null;
  canAccessModule: (moduleKey: string) => boolean;
  atLeast: (moduleKey: string, level: LevelName) => boolean;
  hasPlatform: (perm: string) => boolean;
  refetch: () => void;
}

const PermissionsContext = createContext<PermissionsApi | null>(null);

export function usePermissions(): PermissionsApi {
  const ctx = useContext(PermissionsContext);
  if (!ctx) throw new Error('usePermissions must be used within an <AuthProvider>');
  return ctx;
}

/** Build the query-helper API over a resolved (or absent) permission set. */
function buildPermissionsApi(
  perms: Permissions | null,
  loading: boolean,
  error: boolean,
  refetch: () => void,
): PermissionsApi {
  const moduleLevel = (key: string): LevelName | null => perms?.moduleLevels[key] ?? null;
  return {
    loading,
    error,
    isAdministrator: perms?.isAdministrator ?? false,
    roleName: perms?.roleName ?? null,
    moduleLevel,
    canAccessModule: (key) => moduleLevel(key) !== null,
    atLeast: (key, level) => {
      const current = moduleLevel(key);
      return current !== null && LEVEL_RANK[current] >= LEVEL_RANK[level];
    },
    hasPlatform: (perm) => perms?.platform.includes(perm) ?? false,
    refetch,
  };
}

// ---------------------------------------------------------------------------
// Mock implementation — lets the shell build & run with no Firebase creds.
// The dev switcher picks which SEEDED user to act as, so different ROLES (and
// thus different permission sets from /me) are exercisable locally + in E2E.
// ---------------------------------------------------------------------------

export interface DevUser {
  uid: string;
  label: string;
}

/** The seeded dev users (see backend app/seed.py), one per access shape. */
export const DEV_USERS: readonly DevUser[] = [
  { uid: 'dev-admin', label: 'Administrator' },
  { uid: 'dev-manager', label: 'Challan Manager' },
  { uid: 'dev-operator', label: 'Challan Operator' },
  { uid: 'dev-viewer', label: 'Viewer' },
];

export interface MockDevControls {
  actingUid: string;
  setActingUid: (uid: string) => void;
  users: readonly DevUser[];
}

const MockDevContext = createContext<MockDevControls | null>(null);

export function useMockDevControls(): MockDevControls | null {
  return useContext(MockDevContext);
}

const MOCK_TOKEN = 'mock-id-token.not-a-real-jwt';

export function MockAuthProvider({
  children,
  initialUid = 'dev-admin',
}: {
  children: ReactNode;
  initialUid?: string;
}) {
  const [signedIn, setSignedIn] = useState(true);
  const [actingUid, setActingUid] = useState(initialUid);

  const getToken = useCallback(async () => (signedIn ? MOCK_TOKEN : null), [signedIn]);
  const signIn = useCallback(async (_email: string, _password: string) => {
    setSignedIn(true);
  }, []);
  const signOut = useCallback(async () => setSignedIn(false), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      // Identity is filled in from /me below; keep a lightweight placeholder here.
      user: signedIn ? { uid: actingUid, email: '', name: '' } : null,
      loading: false,
      signIn,
      signOut,
      getToken,
      devUid: signedIn ? actingUid : null,
    }),
    [signedIn, actingUid, signIn, signOut, getToken],
  );

  const devControls = useMemo<MockDevControls>(
    () => ({ actingUid, setActingUid, users: DEV_USERS }),
    [actingUid],
  );

  return (
    <MockDevContext.Provider value={devControls}>
      <AuthAndPermissions value={value}>{children}</AuthAndPermissions>
    </MockDevContext.Provider>
  );
}

/**
 * Wraps the app in the auth context AND fetches `GET /me` to populate permissions.
 * Both the mock and the future Firebase provider render through this, so the
 * permission wiring is identical regardless of how identity is obtained.
 */
export function AuthAndPermissions({
  value,
  children,
}: {
  value: AuthContextValue;
  children: ReactNode;
}) {
  const [perms, setPerms] = useState<Permissions | null>(null);
  const [meUser, setMeUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);

  const signedIn = value.user !== null;
  const devUid = value.devUid;
  const { getToken } = value;

  const refetch = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    if (!signedIn) {
      setPerms(null);
      setMeUser(null);
      setLoading(false);
      setError(false);
      return;
    }
    setLoading(true);
    setError(false);
    void (async () => {
      try {
        const token = await getToken();
        const me = await apiFetch<MeResponse>('/me', { token, devUid });
        if (cancelled) return;
        setPerms({
          roleName: me.role_name,
          isAdministrator: me.is_administrator,
          moduleLevels: me.module_levels,
          platform: me.platform,
        });
        setMeUser({ uid: devUid ?? String(me.id), email: me.email, name: me.name });
      } catch {
        if (cancelled) return;
        setPerms(null);
        setMeUser(null);
        setError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [signedIn, devUid, getToken, nonce]);

  // The exposed user merges the provider identity with the /me identity (email/name).
  const mergedValue = useMemo<AuthContextValue>(
    () => ({ ...value, user: value.user && meUser ? { ...value.user, ...meUser } : value.user }),
    [value, meUser],
  );

  const permsApi = useMemo(
    () => buildPermissionsApi(perms, loading, error, refetch),
    [perms, loading, error, refetch],
  );

  return (
    <AuthContext.Provider value={mergedValue}>
      <PermissionsContext.Provider value={permsApi}>{children}</PermissionsContext.Provider>
    </AuthContext.Provider>
  );
}

const AuthLoading = (
  <div className="grid h-screen place-items-center text-sm text-slate-400">Loading…</div>
);

/**
 * Default provider. A PRODUCTION build uses REAL Firebase auth (email/password); dev + tests use
 * the mock (with the dev role-switcher). Both render through <AuthAndPermissions>, so /me supplies
 * permissions identically regardless of how identity is obtained.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  if (import.meta.env.PROD) {
    return (
      <Suspense fallback={AuthLoading}>
        <FirebaseAuthProvider>{children}</FirebaseAuthProvider>
      </Suspense>
    );
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
