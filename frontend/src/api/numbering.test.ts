import { describe, expect, it } from 'vitest';
import { ALLOCATION_PAGE_SIZE, buildAllocationListQuery } from './numbering';

describe('buildAllocationListQuery', () => {
  it('always sets limit + offset and trims filter params', () => {
    expect(buildAllocationListQuery({ series: ' L ', fy: '', status: 'RESERVED' }, 0)).toBe(
      `?series=L&status=RESERVED&limit=${ALLOCATION_PAGE_SIZE}&offset=0`,
    );
  });

  it('carries the paging offset', () => {
    expect(buildAllocationListQuery({}, ALLOCATION_PAGE_SIZE)).toBe(
      `?limit=${ALLOCATION_PAGE_SIZE}&offset=${ALLOCATION_PAGE_SIZE}`,
    );
  });

  it('omits status when empty', () => {
    expect(buildAllocationListQuery({ status: '' }, 0)).toBe(
      `?limit=${ALLOCATION_PAGE_SIZE}&offset=0`,
    );
  });
});
