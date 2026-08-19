import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider } from '../../ui';
import type { DownloadPreview } from '../../api/challan';
import { DownloadChallans } from './DownloadChallans';

// A controllable stand-in for the react-query preview hook. Each test sets
// `previewReturn` to whatever `useDownloadPreview` should report; the real
// query-string builder + types stay live via importActual.
type PreviewResult = {
  data: DownloadPreview | undefined;
  isFetching: boolean;
  isError: boolean;
  error: Error | null;
};

let previewReturn: PreviewResult;

const downloadUrlMock = vi.fn(async () => ({ truncated: false, filename: 'x' }));

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

function renderScreen() {
  return render(
    <ToastProvider>
      <DownloadChallans />
    </ToastProvider>,
  );
}

/** Fill the three inputs and click Preview so the panel renders. */
function fillAndPreview() {
  fireEvent.change(screen.getByPlaceholderText('e.g. L'), { target: { value: 'L' } });
  fireEvent.change(screen.getByPlaceholderText('e.g. 26-27'), { target: { value: '26-27' } });
  fireEvent.change(screen.getByPlaceholderText('e.g. 10-50, 55, 60'), {
    target: { value: '10-50, 55, 60' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
}

describe('DownloadChallans', () => {
  beforeEach(() => {
    previewReturn = { data: undefined, isFetching: false, isError: false, error: null };
    downloadUrlMock.mockClear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the resolved count and an amber skipped-void note', () => {
    previewReturn = {
      data: {
        series: 'L',
        fy: '26-27',
        count: 3,
        resolved: [
          { number_int: 10, number: 'L/26-27/0010', id: 1 },
          { number_int: 55, number: 'L/26-27/0055', id: 2 },
          { number_int: 60, number: 'L/26-27/0060', id: 3 },
        ],
        skipped_void: [11, 22],
        missing: [],
        errors: [],
      },
      isFetching: false,
      isError: false,
      error: null,
    };
    renderScreen();
    fillAndPreview();

    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText(/will download/)).toBeInTheDocument();
    // The operator must be told which void numbers were dropped.
    expect(screen.getByText(/Skipping voided/)).toBeInTheDocument();
    expect(screen.getByText('11, 22')).toBeInTheDocument();
  });

  it('surfaces spec errors[] as an inline validation message', () => {
    previewReturn = {
      data: {
        series: 'L',
        fy: '26-27',
        count: 0,
        resolved: [],
        skipped_void: [],
        missing: [],
        errors: ['Invalid range: "50-10" (start is after end).'],
      },
      isFetching: false,
      isError: false,
      error: null,
    };
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

    // Before any preview, Download is disabled.
    const downloadBtn = () => screen.getByRole('button', { name: 'Download' });
    expect(downloadBtn()).toBeDisabled();

    // A valid preview enables it.
    previewReturn = {
      data: {
        series: 'L',
        fy: '26-27',
        count: 2,
        resolved: [
          { number_int: 10, number: 'L/26-27/0010', id: 1 },
          { number_int: 11, number: 'L/26-27/0011', id: 2 },
        ],
        skipped_void: [],
        missing: [],
        errors: [],
      },
      isFetching: false,
      isError: false,
      error: null,
    };
    rerender(
      <ToastProvider>
        <DownloadChallans />
      </ToastProvider>,
    );
    fillAndPreview();
    expect(downloadBtn()).toBeEnabled();
  });

  it('keeps Download disabled when the preview resolved to zero challans', () => {
    previewReturn = {
      data: {
        series: 'L',
        fy: '26-27',
        count: 0,
        resolved: [],
        skipped_void: [12],
        missing: [99],
        errors: [],
      },
      isFetching: false,
      isError: false,
      error: null,
    };
    renderScreen();
    fillAndPreview();

    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
    expect(screen.getByText(/Not found in/)).toBeInTheDocument();
  });
});
