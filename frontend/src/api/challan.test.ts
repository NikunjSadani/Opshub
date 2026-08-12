import { describe, expect, it } from 'vitest';
import {
  buildChallanCsvQuery,
  buildChallanListQuery,
  buildChallanSummaryQuery,
  CHALLAN_PAGE_SIZE,
} from './challan';

describe('buildChallanSummaryQuery', () => {
  it('appends only non-empty, trimmed params', () => {
    expect(buildChallanSummaryQuery({ series: '  L ', fy: ' 26-27 ' })).toBe('?series=L&fy=26-27');
  });

  it('omits blank / whitespace-only params', () => {
    expect(buildChallanSummaryQuery({ series: '   ', fy: '' })).toBe('');
    expect(buildChallanSummaryQuery({})).toBe('');
  });

  it('keeps a single provided param', () => {
    expect(buildChallanSummaryQuery({ fy: '26-27' })).toBe('?fy=26-27');
  });
});

describe('buildChallanListQuery', () => {
  it('always sets limit + offset and trims filter params', () => {
    expect(buildChallanListQuery({ series: ' L ', fy: '', status: 'ISSUED' }, 0)).toBe(
      `?series=L&status=ISSUED&limit=${CHALLAN_PAGE_SIZE}&offset=0`,
    );
  });

  it('carries the paging offset', () => {
    expect(buildChallanListQuery({}, CHALLAN_PAGE_SIZE)).toBe(
      `?limit=${CHALLAN_PAGE_SIZE}&offset=${CHALLAN_PAGE_SIZE}`,
    );
  });

  it('omits status when empty', () => {
    expect(buildChallanListQuery({ status: '' }, 0)).toBe(`?limit=${CHALLAN_PAGE_SIZE}&offset=0`);
  });

  it('includes trimmed date_from / date_to when set', () => {
    expect(buildChallanListQuery({ date_from: ' 2026-04-01 ', date_to: '2026-04-30' }, 0)).toBe(
      `?date_from=2026-04-01&date_to=2026-04-30&limit=${CHALLAN_PAGE_SIZE}&offset=0`,
    );
  });

  it('omits blank date bounds', () => {
    expect(buildChallanListQuery({ date_from: '   ', date_to: '' }, 0)).toBe(
      `?limit=${CHALLAN_PAGE_SIZE}&offset=0`,
    );
  });
});

describe('buildChallanCsvQuery', () => {
  it('carries the same filters as the list, without limit/offset', () => {
    expect(
      buildChallanCsvQuery({
        series: ' L ',
        fy: '26-27',
        status: 'ISSUED',
        date_from: '2026-04-01',
        date_to: '2026-04-30',
      }),
    ).toBe('?series=L&fy=26-27&status=ISSUED&date_from=2026-04-01&date_to=2026-04-30');
  });

  it('returns an empty string when no filters are set', () => {
    expect(buildChallanCsvQuery({})).toBe('');
    expect(buildChallanCsvQuery({ series: '  ', status: '' })).toBe('');
  });
});
