import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useDebouncedValue } from './useDebouncedValue';

describe('useDebouncedValue', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('returns the initial value immediately', () => {
    const { result } = renderHook(() => useDebouncedValue('a', 300));
    expect(result.current).toBe('a');
  });

  it('only settles on the latest value after the delay elapses', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 300), {
      initialProps: { v: 'a' },
    });

    rerender({ v: 'ab' });
    rerender({ v: 'abc' });
    // Still the old value before the window closes.
    expect(result.current).toBe('a');

    // Not yet — halfway through the window.
    act(() => vi.advanceTimersByTime(150));
    expect(result.current).toBe('a');

    // The window closes on the last change → one settle to the latest value.
    act(() => vi.advanceTimersByTime(150));
    expect(result.current).toBe('abc');
  });

  it('resets the timer on every change (one settle for a fast typer)', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 300), {
      initialProps: { v: '' },
    });

    for (const v of ['a', 'ab', 'abc', 'abcd']) {
      rerender({ v });
      act(() => vi.advanceTimersByTime(299)); // each keystroke arrives just under the delay
    }
    expect(result.current).toBe('');

    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe('abcd');
  });
});
