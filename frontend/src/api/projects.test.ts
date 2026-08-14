import { describe, expect, it } from 'vitest';
import { buildProjectsQuery } from './projects';

describe('buildProjectsQuery', () => {
  it('appends only non-empty, trimmed params', () => {
    expect(buildProjectsQuery({ client_id: ' c1 ', status: 'ACTIVE', q: '  BRI ' })).toBe(
      '?client_id=c1&status=ACTIVE&q=BRI',
    );
  });

  it('omits blank / whitespace-only params', () => {
    expect(buildProjectsQuery({ client_id: '   ', status: '', q: '' })).toBe('');
    expect(buildProjectsQuery({})).toBe('');
  });

  it('keeps a single provided param', () => {
    expect(buildProjectsQuery({ status: 'ON_HOLD' })).toBe('?status=ON_HOLD');
    expect(buildProjectsQuery({ q: 'scheme' })).toBe('?q=scheme');
  });

  it('url-encodes free-text search', () => {
    expect(buildProjectsQuery({ q: 'a b' })).toBe('?q=a+b');
  });
});
