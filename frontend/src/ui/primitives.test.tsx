import { fireEvent, render, screen } from '@testing-library/react';
import { Button } from './primitives';

describe('Button disabled/loading semantics', () => {
  it('is enabled by default', () => {
    render(<Button>Go</Button>);
    expect(screen.getByRole('button', { name: /go/i })).toBeEnabled();
  });

  it('is disabled while loading', () => {
    render(<Button loading>Go</Button>);
    expect(screen.getByRole('button', { name: /go/i })).toBeDisabled();
  });

  it('honors an explicit disabled when not loading', () => {
    render(<Button disabled>Go</Button>);
    expect(screen.getByRole('button', { name: /go/i })).toBeDisabled();
  });

  // Regression: a loading button with an explicit `disabled={false}` must STILL
  // be disabled (the old `disabled ?? loading` short-circuited and left it
  // clickable → double-submit). This is exactly the shape used at the challan
  // upload/generate/retry sites.
  it('stays disabled while loading even with disabled={false}, and does not fire onClick', () => {
    const onClick = vi.fn();
    render(
      <Button loading disabled={false} onClick={onClick}>
        Go
      </Button>,
    );
    const btn = screen.getByRole('button', { name: /go/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });
});
