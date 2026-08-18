import { useEffect, useState } from 'react';

/**
 * Returns `value` delayed by `delayMs` (default 300). The returned value only
 * settles once `value` has stopped changing for the full delay window, so an
 * input bound to `value` (rendered immediately, responsive to typing) can feed a
 * debounced copy into a query key — firing one request after typing settles
 * instead of one per keystroke.
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(t);
  }, [value, delayMs]);

  return debounced;
}
