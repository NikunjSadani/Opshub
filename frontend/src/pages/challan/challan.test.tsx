import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider, MockAuthProvider } from '../../auth/AuthProvider';
import { ToastProvider } from '../../ui';
import { NewChallan } from './NewChallan';
import { Batches } from './Batches';

/**
 * Build the `GET /me` permission payload for the acting dev user (read off the
 * `X-Dev-Uid` header the mock provider sends). dev-admin/dev-manager get MANAGE
 * on the challan module (so admin-gated actions like Recover / Update master
 * show); everyone else gets OPERATE. Every fetch stub below returns this for /me
 * so the RBAC gates resolve deterministically.
 */
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

function renderNewChallan() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <NewChallan />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderBatches(uid?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Default AuthProvider acts as dev-admin (MANAGE); pass a seeded uid (e.g.
  // 'dev-operator') to exercise the non-admin gates via the mock provider.
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
            <Batches />
          </ToastProvider>,
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('NewChallan upload flow', () => {
  beforeEach(() => {
    // The screen now legitimately fetches the recent-batches list on mount
    // (useBatchesQuery). Return an empty list for that call so "Recent batches"
    // renders its empty state; any OTHER call fails loudly instead of hanging.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/challan/batches')) {
          return new Response('[]', {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the upload form and the blank-template control', async () => {
    renderNewChallan();

    // Upload action + template control are present.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download template/i })).toBeInTheDocument();

    // The upload button is disabled until a file is chosen.
    expect(screen.getByRole('button', { name: /upload & validate/i })).toBeDisabled();

    // Recent batches resolves to its empty state (the one mount-time fetch).
    expect(await screen.findByText(/no batches yet/i)).toBeInTheDocument();
  });

  it('shows a non-blocking warnings notice on a VALIDATED-with-warnings batch and keeps Generate enabled', async () => {
    // Override the mount stub: the POST upload resolves to VALIDATED but carries
    // warnings (message + error_report_file_id). GET recent-batches stays empty.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (url.includes('/challan/batches') && method === 'POST') {
          return new Response(
            JSON.stringify({
              id: 42,
              status: 'VALIDATED',
              challan_count: 3,
              line_count: 7,
              message: '2 warning(s)',
              error_report_file_id: 99,
              zip_file_id: null,
              merged_pdf_file_id: null,
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        if (url.includes('/challan/batches')) {
          return new Response('[]', {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderNewChallan();

    const fileInput = document.getElementById('challan-file') as HTMLInputElement;
    const file = new File(['data'], 'challans.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    fireEvent.change(fileInput, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /upload & validate/i }));

    // The amber warnings notice appears inside the validated block. Match a
    // phrase unique to the notice (the sr-only live region also says
    // "non-blocking", so a looser match would find two elements).
    expect(await screen.findByText(/Review before generating/i)).toBeInTheDocument();
    expect(screen.getByText('2 warning(s)')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /download warnings report/i }),
    ).toBeInTheDocument();

    // Warnings do NOT block: Step 3's Generate button is enabled.
    const generate = screen.getByRole('button', { name: /generate 3 challans/i });
    expect(generate).toBeEnabled();
  });

  it('renders the review panel for a NEEDS_REVIEW batch and blocks generation until every field is decided', async () => {
    const NEEDS_REVIEW_BATCH = {
      id: 55,
      status: 'NEEDS_REVIEW',
      challan_count: 2,
      line_count: 4,
      message: '2 consignee contradiction(s) to review before generating',
      error_report_file_id: 77,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    const DECISIONS = [
      {
        id: 1,
        gstin: '27ABCDE1234F1Z5',
        consignee_name: 'Acme Foods',
        field: 'name',
        stored_value: 'Acme Foods Pvt Ltd',
        uploaded_value: 'Acme Foods',
        choice: 'PENDING',
      },
      {
        id: 2,
        gstin: '27ABCDE1234F1Z5',
        consignee_name: 'Acme Foods',
        field: 'pincode',
        stored_value: '400001',
        uploaded_value: '400002',
        choice: 'PENDING',
      },
    ];
    let patchBody: unknown = null;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });

        if (url.includes('/decisions')) {
          if (method === 'PATCH') {
            patchBody = JSON.parse(String(init?.body));
            // Nothing left PENDING -> batch flips to VALIDATED.
            return json({ ...NEEDS_REVIEW_BATCH, status: 'VALIDATED', error_report_file_id: null });
          }
          return json(DECISIONS);
        }
        if (url.includes('/challan/batches') && method === 'POST') {
          return json(NEEDS_REVIEW_BATCH);
        }
        if (url.includes('/challan/batches')) {
          return json([]);
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderNewChallan();

    const fileInput = document.getElementById('challan-file') as HTMLInputElement;
    const file = new File(['data'], 'challans.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    fireEvent.change(fileInput, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /upload & validate/i }));

    // Review panel + both contradicted fields + the Excel download appear.
    expect(
      await screen.findByText(/this upload disagrees with your saved records/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /download review report \(excel\)/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Consignee name')).toBeInTheDocument();
    expect(screen.getByText('Pincode')).toBeInTheDocument();

    // Step 3's Generate is not available while the batch needs review.
    expect(screen.queryByRole('button', { name: /generate/i })).not.toBeInTheDocument();

    // Save is disabled until every field has a choice.
    const save = screen.getByRole('button', { name: /save decisions & continue/i });
    expect(save).toBeDisabled();

    // The undecided fields carry a text marker (not colour only).
    expect(screen.getAllByText(/not decided/i).length).toBe(2);

    // Choose one field, still disabled; choose the second, now enabled.
    fireEvent.click(document.getElementById('decision-1-THIS_UPLOAD') as HTMLInputElement);
    expect(save).toBeDisabled();
    // Progress affordance updates as fields are decided.
    expect(screen.getByText(/1 of 2 decided/i)).toBeInTheDocument();
    fireEvent.click(document.getElementById('decision-2-REJECT') as HTMLInputElement);
    expect(save).toBeEnabled();
    expect(screen.getByText(/2 of 2 decided/i)).toBeInTheDocument();

    // No UPDATE_MASTER selected -> Save submits directly (no confirm dialog);
    // the batch then flips to VALIDATED.
    fireEvent.click(save);
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({
      decisions: [
        { id: 1, choice: 'THIS_UPLOAD' },
        { id: 2, choice: 'REJECT' },
      ],
    });
    expect(await screen.findByRole('button', { name: /generate 2 challans/i })).toBeInTheDocument();
  });

  it('confirms before a permanent shared-master change, then PATCHes on confirm', async () => {
    const NEEDS_REVIEW_BATCH = {
      id: 55,
      status: 'NEEDS_REVIEW',
      challan_count: 2,
      line_count: 4,
      message: '1 consignee contradiction(s) to review before generating',
      error_report_file_id: 77,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    const DECISIONS = [
      {
        id: 1,
        gstin: '27ABCDE1234F1Z5',
        consignee_name: 'Acme Foods',
        field: 'name',
        stored_value: 'Acme Foods Pvt Ltd',
        uploaded_value: 'Acme Foods',
        choice: 'PENDING',
      },
    ];
    let patchBody: unknown = null;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.includes('/decisions')) {
          if (method === 'PATCH') {
            patchBody = JSON.parse(String(init?.body));
            return json({ ...NEEDS_REVIEW_BATCH, status: 'VALIDATED', error_report_file_id: null });
          }
          return json(DECISIONS);
        }
        if (url.includes('/challan/batches') && method === 'POST') {
          return json(NEEDS_REVIEW_BATCH);
        }
        if (url.includes('/challan/batches')) {
          return json([]);
        }
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderNewChallan();

    const fileInput = document.getElementById('challan-file') as HTMLInputElement;
    const file = new File(['data'], 'challans.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    fireEvent.change(fileInput, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /upload & validate/i }));

    // Pick "Update master" and click Save — a confirm dialog appears, no PATCH yet.
    fireEvent.click(await screen.findByLabelText(/update master/i));
    fireEvent.click(screen.getByRole('button', { name: /save decisions & continue/i }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(
      screen.getByText(/permanently update 1 saved consignee record/i),
    ).toBeInTheDocument();
    expect(patchBody).toBeNull();

    // Confirming sends the PATCH with the UPDATE_MASTER choice.
    fireEvent.click(screen.getByRole('button', { name: /update saved records/i }));
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({ decisions: [{ id: 1, choice: 'UPDATE_MASTER' }] });
  });
});

describe('Batches review affordance', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('exposes a Review action on a NEEDS_REVIEW row that opens the review panel', async () => {
    const NEEDS_REVIEW_BATCH = {
      id: 88,
      status: 'NEEDS_REVIEW',
      challan_count: 1,
      line_count: 2,
      message: '1 consignee contradiction(s) to review before generating',
      error_report_file_id: 77,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    const DECISIONS = [
      {
        id: 9,
        gstin: '27ABCDE1234F1Z5',
        consignee_name: 'Acme Foods',
        field: 'pincode',
        stored_value: '400001',
        uploaded_value: '400002',
        choice: 'PENDING',
      },
    ];

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.includes('/decisions')) return json(DECISIONS);
        if (url.includes('/challan/batches')) return json([NEEDS_REVIEW_BATCH]);
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderBatches();

    // The NEEDS_REVIEW row surfaces a "Review" affordance.
    const review = await screen.findByRole('button', { name: /^review$/i });
    expect(review).toBeInTheDocument();

    // Opening it renders the same review panel with its contradictions.
    fireEvent.click(review);
    expect(
      await screen.findByText(/this upload disagrees with your saved records/i),
    ).toBeInTheDocument();
    expect(await screen.findByText('Pincode')).toBeInTheDocument();
  });

  it('offers Retry generation on a FAILED row and Recover on a GENERATING row (admin)', async () => {
    const FAILED = {
      id: 70,
      status: 'FAILED',
      challan_count: 3,
      line_count: 6,
      message: 'render crashed midway',
      error_report_file_id: null,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    const GENERATING = {
      id: 71,
      status: 'GENERATING',
      challan_count: 2,
      line_count: 4,
      message: null,
      error_report_file_id: null,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    let generateBody: unknown = null;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.includes('/generate') && method === 'POST') {
          generateBody = JSON.parse(String(init?.body));
          return json({ ...FAILED, status: 'GENERATING' });
        }
        if (url.includes('/challan/batches')) return json([FAILED, GENERATING]);
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderBatches();

    // FAILED -> Retry generation; opens a series prompt and POSTs /generate.
    const retry = await screen.findByRole('button', { name: /retry generation/i });
    fireEvent.click(retry);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    // Confirm with the default series.
    fireEvent.click(screen.getAllByRole('button', { name: /retry generation/i }).pop()!);
    await waitFor(() => expect(generateBody).toEqual({ series: 'L' }));

    // GENERATING -> Recover is available to an admin.
    expect(screen.getByRole('button', { name: /^recover$/i })).toBeInTheDocument();
  });

  it('hides Recover from a non-admin on a GENERATING row and shows a hint instead', async () => {
    const GENERATING = {
      id: 72,
      status: 'GENERATING',
      challan_count: 2,
      line_count: 4,
      message: null,
      error_report_file_id: null,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/challan/batches'))
          return new Response(JSON.stringify([GENERATING]), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderBatches('dev-operator');

    expect(await screen.findByText(/an admin can recover it/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^recover$/i })).not.toBeInTheDocument();
  });

  it('disables Update master in the review panel for a non-admin', async () => {
    const NEEDS_REVIEW_BATCH = {
      id: 90,
      status: 'NEEDS_REVIEW',
      challan_count: 1,
      line_count: 2,
      message: '1 consignee contradiction(s) to review before generating',
      error_report_file_id: 77,
      zip_file_id: null,
      merged_pdf_file_id: null,
    };
    const DECISIONS = [
      {
        id: 9,
        gstin: '27ABCDE1234F1Z5',
        consignee_name: 'Acme Foods',
        field: 'pincode',
        stored_value: '400001',
        uploaded_value: '400002',
        choice: 'PENDING',
      },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.includes('/decisions')) return json(DECISIONS);
        if (url.includes('/challan/batches')) return json([NEEDS_REVIEW_BATCH]);
        if (url.endsWith('/me')) return meResponse(init);
        throw new Error(`Unexpected fetch: ${url}`);
      }),
    );

    renderBatches('dev-operator');

    fireEvent.click(await screen.findByRole('button', { name: /^review$/i }));
    // The UPDATE_MASTER radio is present but disabled; the two safe options are not.
    const updateMaster = (await screen.findByLabelText(/update master/i)) as HTMLInputElement;
    expect(updateMaster).toBeDisabled();
    expect(screen.getByLabelText(/this upload only/i)).toBeEnabled();
    expect(screen.getByText(/admin only — choose this upload/i)).toBeInTheDocument();
  });
});
