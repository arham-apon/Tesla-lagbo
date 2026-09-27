import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Button } from './Button';

describe('Button — seven-state contract', () => {
  it('default: clickable, keyboard-focusable', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Reserve Seat 3</Button>);
    const btn = screen.getByRole('button', { name: 'Reserve Seat 3' });
    await userEvent.tab();
    expect(btn).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('disabled: aria-disabled, still focusable so its reason can be read, clicks ignored', async () => {
    const onClick = vi.fn();
    render(
      <>
        <Button onClick={onClick} disabled aria-describedby="why">
          Reserve Seat 3
        </Button>
        <p id="why">Bullet is full (3 / 3).</p>
      </>,
    );
    const btn = screen.getByRole('button', { name: 'Reserve Seat 3' });
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    expect(btn).toHaveAccessibleDescription('Bullet is full (3 / 3).');
    await userEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('loading: aria-busy, loading label exposed, repeated taps ignored', async () => {
    const onClick = vi.fn();
    render(
      <Button onClick={onClick} loading loadingLabel="Securing Seat...">
        Reserve Seat 3
      </Button>,
    );
    const btn = screen.getByRole('button');
    expect(btn).toHaveAttribute('aria-busy', 'true');
    expect(btn).toHaveTextContent('Securing Seat...');
    await userEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('error: aria-invalid and the message linked through aria-describedby', () => {
    render(
      <>
        <Button errorId="err" errorKey={1}>
          Reserve Seat 3
        </Button>
        <p id="err">Seat 3 was claimed 340ms before you.</p>
      </>,
    );
    const btn = screen.getByRole('button', { name: 'Reserve Seat 3' });
    expect(btn).toHaveAttribute('aria-invalid', 'true');
    expect(btn).toHaveAccessibleDescription('Seat 3 was claimed 340ms before you.');
  });
});
