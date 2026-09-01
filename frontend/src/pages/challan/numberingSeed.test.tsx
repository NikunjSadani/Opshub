import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider, MockAuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { Numbering } from './Numbering';

/** Permission payload for the acting dev user — MANAGE for admin/manager, else OPERATE. */
function meResponse(init?: RequestInit): Response {
  const headers = (init?.headers ?? {}) as Record<string, string>;
  const uid = headers['X-Dev-Uid'] ?? 'dev-admin';
  const manage = uid === 'dev-admin' || uid === 'dev-manager';
  return new Response(
    JSON.stringify({
      id: 1,
      email: 'test@example.com',
      name: 'Test User',
      role_id: 1,
      role_name: manage ? 'Administrator' : 'Challan Operator',
      is_administrator: uid === 'dev-admin',
      module_levels: { document_automation: manage ? 'MANAGE' : 'OPERATE' },
      platform: uid === 'dev-admin' ? ['iam', 'settings'] : [],
    }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  );
}

/** Records the last POST /numbering/seed body so the test can assert the payload. */
let seedBody: unknown = null;

function stubFetch() {
  seedBody = null;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.endsWith('/me')) return meResponse(init);
      if (url.includes('/numbering/seed') && method === 'POST') {
        seedBody = JSON.parse(String(init?.body ?? '{}'));
        const b = seedBody as { series: string; fy?: string; last_number: number };
        return new Response(
          JSON.stringify({ series: b.series, fy: b.fy ?? '26-27', last_number: b.last_number }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      if (url.includes('/numbering/counters')) {
        return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (url.includes('/numbering/allocations')) {
        return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
}

function renderNumbering(uid?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const auth = (children: ReactNode) =>
    uid ? (
      <MockAuthProvider initialUid={uid}>{children}</MockAuthProvider>
    ) : (
      <AuthProvider>{children}</AuthProvider>
    );
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {auth(
          <ToastProvider>
            <Numbering />
          </ToastProvider>,
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Numbering — Set starting number (seed)', () => {
  beforeEach(stubFetch);
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('hides the action for a non-MANAGE (Operate) user', async () => {
    renderNumbering('dev-operator');
    // Wait for the page to settle (the empty-counters state renders).
    expect(await screen.findByText(/no counters yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /set starting number/i })).not.toBeInTheDocument();
  });

  it('lets a MANAGE user seed a fresh series with the right payload', async () => {
    renderNumbering(); // default AuthProvider = dev-admin (MANAGE)

    const open = await screen.findByRole('button', { name: /set starting number/i });
    fireEvent.click(open);

    // Scope field queries to the dialog — the page also has "Series"/"Financial year"
    // filter inputs with the same labels.
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^series/i), { target: { value: 'm1' } });
    fireEvent.change(within(dialog).getByLabelText(/financial year/i), { target: { value: '26-27' } });
    fireEvent.change(within(dialog).getByLabelText(/last issued number/i), { target: { value: '0' } });

    // Submit (the footer button, not the header opener).
    fireEvent.click(within(dialog).getByRole('button', { name: /set starting number/i }));

    await waitFor(() =>
      expect(seedBody).toEqual({ series: 'M1', fy: '26-27', last_number: 0 }),
    );
    // Success toast confirms the next number.
    expect(await screen.findByText(/next challan will be 1/i)).toBeInTheDocument();
  });

  it('blocks a non-consecutive FY inline (never posts) and previews the next number', async () => {
    renderNumbering();
    fireEvent.click(await screen.findByRole('button', { name: /set starting number/i }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^series/i), { target: { value: 'M1' } });
    fireEvent.change(within(dialog).getByLabelText(/financial year/i), { target: { value: '26-28' } });
    fireEvent.change(within(dialog).getByLabelText(/last issued number/i), { target: { value: '0' } });

    // Live preview reflects the entered number before any submit (assert on the
    // preview's unbroken tail phrase — the lead is split by a <strong> number).
    expect(within(dialog).getByText(/never reissued/i)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: /set starting number/i }));

    // Inline validation blocks the submit — no POST fired.
    expect(await within(dialog).findByText(/consecutive/i)).toBeInTheDocument();
    expect(seedBody).toBeNull();
  });

  it('omits fy when left blank (defaults to current FY) and upper-cases the series', async () => {
    renderNumbering();
    fireEvent.click(await screen.findByRole('button', { name: /set starting number/i }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^series/i), { target: { value: 'm1' } });
    fireEvent.change(within(dialog).getByLabelText(/last issued number/i), { target: { value: '5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /set starting number/i }));
    await waitFor(() => expect(seedBody).toEqual({ series: 'M1', last_number: 5 }));
  });
});
