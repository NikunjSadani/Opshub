import { describe, expect, it } from 'vitest';
import { buildChallanListQuery, buildChallanSummaryQuery, CHALLAN_PAGE_SIZE } from './challan';

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
});
