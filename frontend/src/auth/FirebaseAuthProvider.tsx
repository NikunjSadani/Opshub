import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getApps, initializeApp } from 'firebase/app';
import {
  EmailAuthProvider,
  getAuth,
  onAuthStateChanged,
  reauthenticateWithCredential,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
  updatePassword,
  type Auth,
  type User as FirebaseUser,
} from 'firebase/auth';
import { firebaseConfig } from '../firebaseConfig';
import { AuthAndPermissions, type AuthContextValue } from './AuthProvider';
import { recordLoginEvent } from '../api/audit';

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
    const cred = await signInWithEmailAndPassword(firebaseAuth(), email.trim(), password);
    // Best-effort audit ping — records THIS explicit sign-in (the backend reads
    // IP + user-agent from the request). Fired ONLY here, never from
    // onAuthStateChanged (a restored session), and never allowed to block or
    // fail the login: any error (network / 403 / non-2xx) is swallowed.
    try {
      const token = await cred.user.getIdToken();
      await recordLoginEvent(token);
    } catch {
      /* swallow — the audit ping must never break a successful sign-in */
    }
  }, []);

  const signOut = useCallback(async () => {
    await firebaseSignOut(firebaseAuth());
  }, []);

  const sendPasswordReset = useCallback(async (resetEmail: string) => {
    await sendPasswordResetEmail(firebaseAuth(), resetEmail.trim());
  }, []);

  // Change the signed-in user's own password in place. updatePassword requires a RECENT
  // login; on `auth/requires-recent-login` we reauthenticate with the current password
  // (proving the user knows it) and retry. Firebase surfaces a wrong current password as
  // `auth/wrong-password` / `auth/invalid-credential` from the reauth step — the caller
  // shows that message plainly.
  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    const current = firebaseAuth().currentUser;
    if (!current || !current.email) {
      throw new Error('You must be signed in to change your password.');
    }
    try {
      await updatePassword(current, newPassword);
    } catch (err) {
      const code = (err as { code?: string })?.code;
      if (code !== 'auth/requires-recent-login') throw err;
      const credential = EmailAuthProvider.credential(current.email, currentPassword);
      await reauthenticateWithCredential(current, credential);
      await updatePassword(current, newPassword);
    }
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
      sendPasswordReset,
      changePassword,
      devUid: null,
    }),
    [user, ready, signIn, signOut, getToken, sendPasswordReset, changePassword],
  );

  return <AuthAndPermissions value={value}>{children}</AuthAndPermissions>;
}
