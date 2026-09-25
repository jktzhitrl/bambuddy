import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ZahlFeld } from '../../pages/LagerAutodruckPage';

function Probe({ leerErlaubt = false }: { leerErlaubt?: boolean }) {
  const [wert, setWert] = useState<number | null>(1);
  return (
    <>
      <ZahlFeld min={1} leerErlaubt={leerErlaubt} wert={wert} onWert={setWert} className="feld" />
      <output data-testid="wert">{String(wert)}</output>
    </>
  );
}

describe('ZahlFeld (Stück pro Druck)', () => {
  it('die 1 lässt sich löschen und durch 4 ersetzen', () => {
    render(<Probe />);
    const feld = screen.getByRole('spinbutton') as HTMLInputElement;
    fireEvent.change(feld, { target: { value: '' } });
    expect(feld.value).toBe('');
    fireEvent.change(feld, { target: { value: '4' } });
    expect(screen.getByTestId('wert').textContent).toBe('4');
  });

  it('leer verlassen: Minimum, bzw. leer wenn erlaubt', () => {
    const { unmount } = render(<Probe />);
    let feld = screen.getByRole('spinbutton') as HTMLInputElement;
    fireEvent.change(feld, { target: { value: '' } });
    fireEvent.blur(feld);
    expect(feld.value).toBe('1');
    unmount();
    render(<Probe leerErlaubt />);
    feld = screen.getByRole('spinbutton') as HTMLInputElement;
    fireEvent.change(feld, { target: { value: '' } });
    fireEvent.blur(feld);
    expect(screen.getByTestId('wert').textContent).toBe('null');
  });
});
