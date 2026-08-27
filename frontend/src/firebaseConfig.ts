// Firebase web config for OpsHub (project opshub-506704).
//
// These values are PUBLIC BY DESIGN — the apiKey identifies the Firebase project, it is not a
// secret. Access is protected by Firebase Auth + the backend's server-side ID-token verification
// (app/platform/auth.py), not by keeping this hidden. Safe to ship in the built frontend.
export const firebaseConfig = {
  apiKey: 'AIzaSyAazQEnBQrUbisdKNyFtihAEdyD6U5O6ds',
  authDomain: 'opshub-506704.firebaseapp.com',
  projectId: 'opshub-506704',
  storageBucket: 'opshub-506704.firebasestorage.app',
  messagingSenderId: '310561620535',
  appId: '1:310561620535:web:819a58dc8797f1f5ce35b2',
} as const;
