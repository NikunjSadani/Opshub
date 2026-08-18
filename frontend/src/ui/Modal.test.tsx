import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Modal } from './Modal';

// Reproduces the cross-cutting focus bug: a controlled field's onChange re-renders
// the parent, which (with an inline onClose) changed the modal's focus effect deps
// and yanked focus back to the FIRST field on every keystroke. The fix pins onClose
// in a ref so the effect depends only on `open`.
function Harness() {
  const [v, setV] = useState('');
  return (
    // inline onClose => a new function identity on every parent render (the real case)
    <Modal open title="t" onClose={() => {}}>
      <input aria-label="first" />
      <input aria-label="second" value={v} onChange={(e) => setV(e.target.value)} />
    </Modal>
  );
}

describe('Modal focus stability', () => {
  it('keeps focus on a later field while typing (no jump to the first field)', () => {
    render(<Harness />);
    const second = screen.getByLabelText('second') as HTMLInputElement;
    second.focus();
    expect(document.activeElement).toBe(second);

    // Typing re-renders the parent (new onClose identity). Focus must stay put.
    fireEvent.change(second, { target: { value: 'Q' } });

    expect(document.activeElement).toBe(second);
    expect(second.value).toBe('Q');
  });
});
