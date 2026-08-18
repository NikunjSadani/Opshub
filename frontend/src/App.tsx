import { useState, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthProvider';
import { AppShell } from './components/AppShell';
import { Dashboard } from './pages/Dashboard';
import { Login } from './pages/Login';
import { ModulePlaceholder } from './pages/ModulePlaceholder';
import { NotFound } from './pages/NotFound';
import { ChallanModule } from './pages/challan/ChallanModule';
import { ProjectsModule } from './pages/projects/ProjectsModule';
import { UsersModule } from './pages/users/UsersModule';
import { RequireRole } from './auth/RequireRole';
import { ToastProvider } from './ui';

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
    },
  });
}

/** Gate that bounces unauthenticated users to /login, preserving intent. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <div className="grid h-screen place-items-center text-sm text-slate-400">Loading…</div>;
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        {/*
          Access to a module is governed by the backend's PER-USER module-access
          list (surfaced via GET /modules and used to build the nav), NOT by role
          — so no role allowlist is gated here. The backend enforces every call;
          a future RequireModule guard (reading the modules list) is the correct
          FE affordance for direct-URL hits. RequireRole (see its file) is for
          genuinely role-scoped surfaces; the dev role switcher exercises it.
        */}
        <Route path="/m/document_automation/*" element={<ChallanModule />} />
        <Route path="/m/projects/*" element={<ProjectsModule />} />
        {/*
          User Management is a genuinely role-scoped admin surface (unlike the
          per-user module grants above), so it's gated by RequireRole. The
          backend enforces ADMIN on every /users call; this just avoids a
          dead-end for a non-admin who hits the URL directly.
        */}
        <Route
          path="/admin/users"
          element={
            <RequireRole allow={['ADMIN']}>
              <UsersModule />
            </RequireRole>
          }
        />
        <Route path="/m/:key" element={<ModulePlaceholder />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/**
 * Root application. Wires the auth provider (mock today, Firebase later),
 * React Query, and the router. Rendering `<App />` is enough for tests.
 */
export default function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ToastProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
