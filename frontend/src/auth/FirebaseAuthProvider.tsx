import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getApps, initializeApp } from 'firebase/app';
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
  type Auth,
  type User as FirebaseUser,
} from 'firebase/auth';
import { firebaseConfig } from '../firebaseConfig';
import { AuthAndPermissions, type AuthContextValue } from './AuthProvider';

/**
 * Real auth provider — Firebase email/password. Renders through <AuthAndPermissions>, so GET /me
 * supplies permissions exactly as it does for the mock. Identity + the ID token come from
 * firebase/auth; the backend verifies that token server-side (app/platform/auth.py) and maps the
 * uid to an ACTIVE app user row (deny-by-default). devUid is always null under real auth.
 *
 * Loaded only in production (code-split from AuthProvider), so the Firebase SDK never enters the
 * dev/test bundle. The app is initialised lazily (once) here — not at module import.
 */
let _auth: Auth | null = null;
function firebaseAuth(): Auth {
  if (_auth === null) {
    const app = getApps()[0] ?? initializeApp(firebaseConfig);
    _auth = getAuth(app);
  }
  return _auth;
}

export function FirebaseAuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<FirebaseUser | null>(() => firebaseAuth().currentUser);
  // `ready` is false until Firebase resolves the FIRST auth state (checking a persisted session),
  // so we surface loading=true and never flash the login screen on a hard refresh.
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(firebaseAuth(), (u) => {
      setUser(u);
      setReady(true);
    });
    return unsubscribe;
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    await signInWithEmailAndPassword(firebaseAuth(), email.trim(), password);
  }, []);

  const signOut = useCallback(async () => {
    await firebaseSignOut(firebaseAuth());
  }, []);

  // Firebase caches + auto-refreshes the ID token; getIdToken() returns a fresh one each call.
  const getToken = useCallback(async () => {
    const current = firebaseAuth().currentUser;
    return current ? current.getIdToken() : null;
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: user
        ? { uid: user.uid, email: user.email ?? '', name: user.displayName ?? '' }
        : null,
      loading: !ready,
      signIn,
      signOut,
      getToken,
      devUid: null,
    }),
    [user, ready, signIn, signOut, getToken],
  );

  return <AuthAndPermissions value={value}>{children}</AuthAndPermissions>;
}
