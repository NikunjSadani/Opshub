import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';

/**
 * A reusable, accessible, searchable single-select combobox.
 *
 * Built for lists of HUNDREDS of options: it filters by a case-insensitive
 * substring as you type and only renders the first ~50 matches (with a subtle
 * "refine" hint when truncated) so the DOM stays small. No external libraries —
 * plain React state + a click-outside handler — to stay CSP-friendly.
 *
 * Styling mirrors TextField/SelectField in ./form.tsx (same CONTROL classes,
 * border, focus ring and error state) so it lines up with the other controls.
 */

// Keep these in lockstep with ./form.tsx so the combobox is visually identical
// to TextField/SelectField.
const CONTROL =
  'w-full rounded-md border bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500/40';
const ok = 'border-slate-300 focus:border-brand-500';
const bad = 'border-rose-400 focus:border-rose-500';

/** Cap on how many matching options we actually render, for performance. */
const MAX_RENDERED = 50;

export interface SearchableSelectOption {
  value: string;
  label: string;
}

export interface SearchableSelectProps {
  label?: string;
  value: string | null;
  onChange: (value: string) => void;
  options: SearchableSelectOption[];
  placeholder?: string;
  disabled?: boolean;
  error?: string;
  required?: boolean;
  id?: string;
  hint?: string;
  /**
   * Notified with the current query text whenever it changes (including resets to
   * ''). Lets a caller drive a SERVER-side search (debounce + fetch) while the
   * built-in client-side substring filter keeps working over whatever `options`
   * are supplied. Optional — omitting it preserves the plain client-only behavior.
   */
  onQueryChange?: (query: string) => void;
  /**
   * For an OPTIONAL picker: the label of a "none" option prepended to the list (value
   * `''`), so the user can clear the selection back to nothing (e.g. "No PO — match
   * later"). Omit for a required picker — then there is no empty choice.
   */
  noneLabel?: string;
}

/** Mirrors the (non-exported) Field wrapper in ./form.tsx, but associates the
 * label with the input via htmlFor/id instead of wrapping it — the listbox must
 * not live inside a <label>. When no `label` is given, renders children bare. */
function Field({
  htmlFor,
  label,
  required,
  error,
  hint,
  children,
}: {
  htmlFor: string;
  label?: string;
  required?: boolean;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  const meta = error ? (
    <span className="mt-1 block text-xs text-rose-600">{error}</span>
  ) : hint ? (
    <span className="mt-1 block text-xs text-slate-400">{hint}</span>
  ) : null;

  if (!label) {
    return (
      <div className="block">
        {children}
        {meta}
      </div>
    );
  }

  return (
    <div className="block">
      <label htmlFor={htmlFor} className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
        {label}
        {required && <span className="text-rose-500">*</span>}
      </label>
      {children}
      {meta}
    </div>
  );
}

export function SearchableSelect({
  label,
  value,
  onChange,
  options,
  placeholder,
  disabled,
  error,
  required,
  id,
  hint,
  onQueryChange,
  noneLabel,
}: SearchableSelectProps) {
  const reactId = useId();
  const baseId = id ?? `ss-${reactId}`;
  const listboxId = `${baseId}-listbox`;
  const optionId = (index: number) => `${baseId}-opt-${index}`;

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // An OPTIONAL picker prepends a "none" option (value '') so the selection can be cleared.
  const opts = useMemo<SearchableSelectOption[]>(
    () => (noneLabel != null ? [{ value: '', label: noneLabel }, ...options] : options),
    [noneLabel, options],
  );

  const selectedLabel = useMemo(
    () => opts.find((o) => o.value === value)?.label ?? '',
    [opts, value],
  );

  // Case-insensitive substring filter, capped for performance.
  const { visible, truncated } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = q ? opts.filter((o) => o.label.toLowerCase().includes(q)) : opts;
    return { visible: matches.slice(0, MAX_RENDERED), truncated: matches.length > MAX_RENDERED };
  }, [opts, query]);

  // Keep the active index in range as the filtered list changes.
  useEffect(() => {
    setActiveIndex((i) => (visible.length === 0 ? 0 : Math.min(i, visible.length - 1)));
  }, [visible.length]);

  // Notify the caller of the current query text (for an optional server-side search),
  // via a ref so a caller passing an inline function doesn't re-run this on every render.
  const onQueryChangeRef = useRef(onQueryChange);
  useEffect(() => {
    onQueryChangeRef.current = onQueryChange;
  });
  useEffect(() => {
    onQueryChangeRef.current?.(query);
  }, [query]);

  // Close on any click outside the component.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery('');
      }
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  // Scroll the active option into view (guarded — scrollIntoView is a no-op/absent in jsdom).
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`#${CSS.escape(optionId(activeIndex))}`);
    el?.scrollIntoView?.({ block: 'nearest' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIndex, open]);

  function openList() {
    if (disabled) return;
    setOpen(true);
    setQuery('');
    // Preselect the currently-selected option if it's in view.
    const selIdx = opts.findIndex((o) => o.value === value);
    setActiveIndex(selIdx >= 0 && selIdx < MAX_RENDERED ? selIdx : 0);
  }

  function commit(option: SearchableSelectOption) {
    // Focus already stays on the input (mousedown is preventDefault'd; Enter fires
    // while focused), so we deliberately DON'T re-focus here — doing so would
    // re-trigger onFocus -> openList and immediately reopen the list we just closed.
    onChange(option.value);
    setQuery('');
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (disabled) return;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        if (!open) {
          openList();
        } else if (visible.length > 0) {
          setActiveIndex((i) => (i + 1) % visible.length);
        }
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (!open) {
          openList();
        } else if (visible.length > 0) {
          setActiveIndex((i) => (i - 1 + visible.length) % visible.length);
        }
        break;
      case 'Enter':
        if (open && visible[activeIndex]) {
          e.preventDefault();
          commit(visible[activeIndex]);
        }
        break;
      case 'Escape':
        if (open) {
          e.preventDefault();
          setOpen(false);
          setQuery('');
        }
        break;
      default:
        break;
    }
  }

  const displayValue = open ? query : selectedLabel;
  const activeDescendant = open && visible[activeIndex] ? optionId(activeIndex) : undefined;

  return (
    <Field htmlFor={baseId} label={label} required={required} error={error} hint={hint}>
      <div ref={rootRef} className="relative">
        <input
          ref={inputRef}
          id={baseId}
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={activeDescendant}
          aria-required={required || undefined}
          aria-invalid={error ? true : undefined}
          autoComplete="off"
          disabled={disabled}
          required={required}
          placeholder={placeholder}
          value={displayValue}
          onChange={(e) => {
            setQuery(e.target.value);
            if (!open) setOpen(true);
            setActiveIndex(0);
          }}
          onFocus={openList}
          onClick={openList}
          onKeyDown={onKeyDown}
          className={`${CONTROL} ${error ? bad : ok} disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400`}
        />

        {open && (
          <ul
            ref={listRef}
            id={listboxId}
            role="listbox"
            className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-slate-200 bg-white py-1 text-sm shadow-lg"
          >
            {visible.length === 0 ? (
              <li className="px-3 py-2 text-slate-400" role="presentation">
                No matches
              </li>
            ) : (
              visible.map((option, index) => {
                const isActive = index === activeIndex;
                const isSelected = option.value === value;
                return (
                  <li
                    key={option.value}
                    id={optionId(index)}
                    role="option"
                    aria-selected={isSelected}
                    // onMouseDown (not onClick) so selection fires before the
                    // input's blur/click-outside can close the list first.
                    onMouseDown={(e) => {
                      e.preventDefault();
                      commit(option);
                    }}
                    onMouseEnter={() => setActiveIndex(index)}
                    className={`flex cursor-pointer items-center justify-between px-3 py-2 ${
                      isActive ? 'bg-brand-50 text-brand-700' : 'text-slate-700'
                    }`}
                  >
                    <span className="truncate">{option.label}</span>
                    {isSelected && (
                      <span aria-hidden="true" className="ml-2 text-brand-600">
                        ✓
                      </span>
                    )}
                  </li>
                );
              })
            )}
            {truncated && (
              <li className="px-3 py-1.5 text-xs text-slate-400" role="presentation">
                …refine to see more
              </li>
            )}
          </ul>
        )}
      </div>
    </Field>
  );
}
