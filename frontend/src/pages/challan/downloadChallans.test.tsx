import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider } from '../../ui';
import type { DownloadPreview } from '../../api/challan';
import type { DownloadResult } from '../../api/client';
import { DownloadChallans } from './DownloadChallans';

// A controllable stand-in for the react-query preview hook. Each test sets
// `previewReturn` to whatever `useDownloadPreview` should report; the real
// query-string builder + types stay live via importActual.
type PreviewResult = {
  data: DownloadPreview | undefined;
  isFetching: boolean;
  isError: boolean;
  error: Error | null;
  refetch: ReturnType<typeof vi.fn>;
};

let previewReturn: PreviewResult;

// Default: a clean download that skips nothing.
let downloadResult: DownloadResult = {
  truncated: false,
  filename: 'challans.zip',
  skippedVoid: [],
  skippedUnavailable: [],
};
const downloadUrlMock = vi.fn(
  async (_path: string, _fallbackName?: string) => downloadResult,
);

vi.mock('../../api/challan', async (importActual) => {
  const actual = await importActual<typeof import('../../api/challan')>();
  return {
    ...actual,
    useDownloadPreview: () => previewReturn,
  };
});

vi.mock('../../api/client', () => ({
  useApi: () => ({ downloadUrl: downloadUrlMock }),
}));

/** Build a preview payload with sensible empty defaults. */
function preview(over: Partial<DownloadPreview>): DownloadPreview {
  return {
    series: 'L',
    fy: '26-27',
    count: 0,
    resolved: [],
    skipped_void: [],
    no_pdf: [],
    missing: [],
    errors: [],
    ...over,
  };
}

function ok(data: DownloadPreview): PreviewResult {
  return { data, isFetching: false, isError: false, error: null, refetch: vi.fn() };
}

function renderScreen() {
  return render(
    <ToastProvider>
      <DownloadChallans />
    </ToastProvider>,
  );
}

/** Fill the three inputs and click Preview so the panel renders. */
function fillAndPreview(spec = '10-50, 55, 60') {
  fireEvent.change(screen.getByPlaceholderText('e.g. L'), { target: { value: 'L' } });
  fireEvent.change(screen.getByPlaceholderText('e.g. 26-27'), { target: { value: '26-27' } });
  fireEvent.change(screen.getByPlaceholderText('e.g. 10-50, 55, 60'), {
    target: { value: spec },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
}

describe('DownloadChallans', () => {
  beforeEach(() => {
    previewReturn = {
      data: undefined,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
    downloadResult = {
      truncated: false,
      filename: 'challans.zip',
      skippedVoid: [],
      skippedUnavailable: [],
    };
    downloadUrlMock.mockClear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the resolved count and an amber skipped-void note', () => {
    previewReturn = ok(
      preview({
        count: 3,
        resolved: [
          { number_int: 10, number: 'L/26-27/0010', id: 1 },
          { number_int: 55, number: 'L/26-27/0055', id: 2 },
          { number_int: 60, number: 'L/26-27/0060', id: 3 },
        ],
        skipped_void: [11, 22],
      }),
    );
    renderScreen();
    fillAndPreview();

    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText(/will download/)).toBeInTheDocument();
    // The operator must be told which void numbers were dropped.
    expect(screen.getByText(/Skipping voided/)).toBeInTheDocument();
    expect(screen.getByText('11, 22')).toBeInTheDocument();
  });

  it('reports ISSUED-but-unrendered challans (no_pdf) so the count never over-promises', () => {
    previewReturn = ok(
      preview({
        count: 1,
        resolved: [{ number_int: 10, number: 'L/26-27/0010', id: 1 }],
        no_pdf: [11],
      }),
    );
    renderScreen();
    fillAndPreview();

    // Count reflects only the downloadable one...
    expect(screen.getByText('1')).toBeInTheDocument();
    // ...and the un-rendered challan is called out, not silently dropped.
    expect(screen.getByText(/No downloadable PDF yet/)).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
  });

  it('surfaces spec errors[] as an inline validation message', () => {
    previewReturn = ok(
      preview({ errors: ['Invalid range: "50-10" (start is after end).'] }),
    );
    renderScreen();
    fillAndPreview();

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Invalid range: "50-10" (start is after end).',
    );
    // With errors present the count line is not shown.
    expect(screen.queryByText(/will download/)).not.toBeInTheDocument();
  });

  it('disables Download until a valid preview with count > 0', () => {
    const { rerender } = renderScreen();

    const downloadBtn = () => screen.getByRole('button', { name: 'Download' });
    expect(downloadBtn()).toBeDisabled();

    previewReturn = ok(
      preview({
        count: 2,
        resolved: [
          { number_int: 10, number: 'L/26-27/0010', id: 1 },
          { number_int: 11, number: 'L/26-27/0011', id: 2 },
        ],
      }),
    );
    rerender(
      <ToastProvider>
        <DownloadChallans />
      </ToastProvider>,
    );
    fillAndPreview();
    expect(downloadBtn()).toBeEnabled();
  });

  it('an only-voids selection shows "nothing to download" (no contradictory count)', () => {
    previewReturn = ok(preview({ count: 0, skipped_void: [12, 13] }));
    renderScreen();
    fillAndPreview();

    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
    expect(screen.getByText(/Nothing to download from this selection/)).toBeInTheDocument();
    // It must NOT claim "0 challans will download".
    expect(screen.queryByText(/will download/)).not.toBeInTheDocument();
    // The voided numbers are still named.
    expect(screen.getByText('12, 13')).toBeInTheDocument();
  });

  it('offers a retry when the preview request itself fails', () => {
    const refetch = vi.fn();
    previewReturn = {
      data: undefined,
      isFetching: false,
      isError: true,
      error: new Error('network down'),
      refetch,
    };
    renderScreen();
    fillAndPreview();

    // A failed preview is recoverable — the disabled Download hint is honest about it.
    expect(screen.getByText(/Preview failed/)).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: /retry|try again/i });
    fireEvent.click(retry);
    expect(refetch).toHaveBeenCalled();
  });

  it('downloads exactly the previewed params, even after editing an input first', async () => {
    previewReturn = ok(
      preview({ count: 1, resolved: [{ number_int: 10, number: 'L/26-27/0010', id: 1 }] }),
    );
    renderScreen();
    fillAndPreview('10');
    // Editing the spec AFTER previewing must re-disable Download (no stale-spec download).
    fireEvent.change(screen.getByPlaceholderText('e.g. 10-50, 55, 60'), {
      target: { value: '999' },
    });
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();

    // Re-preview the new spec, then download — the query carries the submitted spec.
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(downloadUrlMock).toHaveBeenCalledTimes(1);
    expect(downloadUrlMock.mock.calls[0][0]).toContain('spec=999');
  });

  it('toasts when the server skipped more at download time than the preview showed', async () => {
    previewReturn = ok(
      preview({
        count: 2,
        resolved: [
          { number_int: 10, number: 'L/26-27/0010', id: 1 },
          { number_int: 11, number: 'L/26-27/0011', id: 2 },
        ],
        skipped_void: [],
      }),
    );
    // Between preview and download, 11 got voided and 10's PDF vanished.
    downloadResult = {
      truncated: false,
      filename: 'challans.zip',
      skippedVoid: [11],
      skippedUnavailable: [10],
    };
    renderScreen();
    fillAndPreview();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    // The reconciliation toast (role=status) must name the drift, never skip silently.
    expect(await screen.findByText(/voided since preview: 11/)).toBeInTheDocument();
    expect(screen.getByText(/no downloadable PDF: 10/)).toBeInTheDocument();
  });
});
