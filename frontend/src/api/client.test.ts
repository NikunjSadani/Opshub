import { describe, expect, it } from 'vitest';
import { detailMessage } from './client';

describe('detailMessage', () => {
  it('returns a plain-string detail as-is (409/403/404)', () => {
    expect(detailMessage({ detail: 'brand+state already exists' })).toBe(
      'brand+state already exists',
    );
  });

  it('joins a pydantic 422 array with field prefixes (never "[object Object]")', () => {
    const body = {
      detail: [
        { loc: ['body', 'gstin'], msg: 'GSTIN is invalid' },
        { loc: ['query', 'series'], msg: 'String should have at most 8 characters' },
      ],
    };
    const msg = detailMessage(body);
    expect(msg).toBe('gstin: GSTIN is invalid; series: String should have at most 8 characters');
    expect(msg).not.toContain('[object Object]');
  });

  it('drops a bare "body" loc (cross-field validators)', () => {
    expect(detailMessage({ detail: [{ loc: ['body'], msg: 'state does not match GSTIN' }] })).toBe(
      'state does not match GSTIN',
    );
  });

  it('returns undefined when there is no usable detail', () => {
    expect(detailMessage({})).toBeUndefined();
    expect(detailMessage(null)).toBeUndefined();
  });
});
