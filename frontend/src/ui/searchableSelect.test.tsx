import { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { SearchableSelect, type SearchableSelectOption } from './SearchableSelect';

const FRUITS: SearchableSelectOption[] = [
  { value: 'apple', label: 'Apple' },
  { value: 'apricot', label: 'Apricot' },
  { value: 'banana', label: 'Banana' },
  { value: 'blueberry', label: 'Blueberry' },
  { value: 'cherry', label: 'Cherry' },
];

/** Controlled harness mirroring real usage. */
function Harness({
  options = FRUITS,
  initial = null,
  onChange,
}: {
  options?: SearchableSelectOption[];
  initial?: string | null;
  onChange?: (v: string) => void;
}) {
  const [value, setValue] = useState<string | null>(initial);
  return (
    <SearchableSelect
      label="Fruit"
      value={value}
      options={options}
      onChange={(v) => {
        setValue(v);
        onChange?.(v);
      }}
      placeholder="Pick a fruit"
    />
  );
}

function makeMany(n: number): SearchableSelectOption[] {
  return Array.from({ length: n }, (_, i) => ({ value: `opt-${i}`, label: `Option ${i}` }));
}

describe('SearchableSelect', () => {
  it("renders the selected option's label in the input when closed", () => {
    render(<Harness initial="banana" />);
    const input = screen.getByRole('combobox') as HTMLInputElement;
    expect(input.value).toBe('Banana');
    // Closed: no listbox in the DOM.
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens on focus and filters the list by case-insensitive substring as you type', () => {
    render(<Harness />);
    const input = screen.getByRole('combobox');

    fireEvent.focus(input);
    expect(input).toHaveAttribute('aria-expanded', 'true');
    // All five options shown initially.
    expect(screen.getAllByRole('option')).toHaveLength(5);

    // Typing "ap" (lowercase) matches Apple + Apricot only.
    fireEvent.change(input, { target: { value: 'ap' } });
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(2);
    expect(options.map((o) => o.textContent)).toEqual(['Apple', 'Apricot']);
  });

  it('selects on click: calls onChange with the value, shows the label, and closes', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const input = screen.getByRole('combobox') as HTMLInputElement;

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'blue' } });
    const option = screen.getByRole('option', { name: /blueberry/i });
    fireEvent.mouseDown(option);

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('blueberry');
    expect(input.value).toBe('Blueberry');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('keyboard: ArrowDown moves the active option and Enter selects it', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const input = screen.getByRole('combobox') as HTMLInputElement;

    fireEvent.focus(input); // opens, active = first (Apple)
    fireEvent.keyDown(input, { key: 'ArrowDown' }); // -> Apricot
    fireEvent.keyDown(input, { key: 'ArrowDown' }); // -> Banana
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onChange).toHaveBeenCalledWith('banana');
    expect(input.value).toBe('Banana');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('marks the active option via aria-activedescendant and the selected one via aria-selected', () => {
    render(<Harness initial="banana" />);
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);

    // The open list points aria-activedescendant at some option id.
    const active = input.getAttribute('aria-activedescendant');
    expect(active).toBeTruthy();

    const listbox = screen.getByRole('listbox');
    const selected = within(listbox).getByRole('option', { name: /banana/i });
    expect(selected).toHaveAttribute('aria-selected', 'true');
  });

  it('Escape closes the list without changing the value', () => {
    const onChange = vi.fn();
    render(<Harness initial="apple" onChange={onChange} />);
    const input = screen.getByRole('combobox') as HTMLInputElement;

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'zzz' } });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    // Reverts to the selected label, not the abandoned query.
    expect(input.value).toBe('Apple');
  });

  it('caps a large option list to the first 50 matches and shows a refine hint', () => {
    render(<Harness options={makeMany(500)} />);
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);

    // Only 50 options rendered out of 500.
    expect(screen.getAllByRole('option')).toHaveLength(50);
    expect(screen.getByText(/refine to see more/i)).toBeInTheDocument();

    // Narrowing the query below the cap removes the hint.
    fireEvent.change(input, { target: { value: 'Option 123' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.queryByText(/refine to see more/i)).not.toBeInTheDocument();
  });

  it('respects disabled: stays closed and shows no listbox on focus', () => {
    render(
      <SearchableSelect label="Fruit" value={null} options={FRUITS} onChange={() => {}} disabled />,
    );
    const input = screen.getByRole('combobox');
    expect(input).toBeDisabled();
    fireEvent.focus(input);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('calls onQueryChange with the query text as you type (for a server-side search)', () => {
    const onQueryChange = vi.fn();
    render(
      <SearchableSelect
        label="Fruit"
        value={null}
        options={FRUITS}
        onChange={() => {}}
        onQueryChange={onQueryChange}
      />,
    );
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ap' } });

    // The caller is notified of the current query so it can fetch server-side.
    expect(onQueryChange).toHaveBeenCalledWith('ap');
    // The built-in client-side filter still narrows the supplied options.
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Apple', 'Apricot']);
  });

  it('renders the error message and marks the input invalid', () => {
    render(
      <SearchableSelect label="Fruit" value={null} options={FRUITS} onChange={() => {}} error="Required" />,
    );
    expect(screen.getByText('Required')).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-invalid', 'true');
  });
});
