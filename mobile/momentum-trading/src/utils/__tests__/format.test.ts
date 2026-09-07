import {
  DASH,
  fmtMoney,
  fmtSignedMoney,
  fmtPct,
  fmtVolume,
  fmtQuantity,
  fmtConfidence,
  fmtTime,
  changeColor,
  isUsableConfidence,
} from '../format';

// Every formatter must reject non-finite input. NaN and Infinity are `typeof
// 'number'`, so a `typeof` check alone lets "$NaN" reach a screen where a user
// reads it as their own money — the same class of defect as a fabricated
// figure. These cases are the regression guard for that.
const NON_FINITE = [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY];
const MISSING = [undefined, null] as const;

describe('numeric formatters reject unusable input', () => {
  const numeric = { fmtMoney, fmtSignedMoney, fmtPct, fmtVolume, fmtQuantity, fmtConfidence };

  for (const [name, fn] of Object.entries(numeric)) {
    it(`${name} returns an em dash for non-finite values`, () => {
      for (const value of NON_FINITE) expect(fn(value)).toBe(DASH);
    });

    it(`${name} returns an em dash for missing values`, () => {
      for (const value of MISSING) expect(fn(value)).toBe(DASH);
    });
  }
});

// `fmtMoney`/`fmtSignedMoney` format through `toLocaleString(undefined, ...)`,
// which follows the runtime's locale — correct for a mobile app, but it means
// a hardcoded '$1,234.50' would fail on a machine set to, say, de-DE, where
// the same value renders '1.234,50'. Asserting a fixed string here would make
// the suite pass or fail on the runner's locale rather than on the code.
//
// So the digits are built with the same locale call, and the parts that are
// the actual behaviour under test — the '$' prefix, the explicit +/- sign,
// and two-decimal precision — are asserted separately and exactly.
const digits = (n: number) =>
  n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

describe('formatting of real values', () => {
  it('prefixes money with $ and always shows two decimals', () => {
    expect(fmtMoney(1234.5)).toBe(`$${digits(1234.5)}`);
    expect(fmtMoney(0)).toBe(`$${digits(0)}`);
    expect(fmtMoney(1234.5)).toMatch(/^\$/);
    expect(fmtMoney(1234.5)).toMatch(/\d{2}$/);
  });

  it('always shows an explicit sign for signed money, and never "$-"', () => {
    expect(fmtSignedMoney(100)).toBe(`+$${digits(100)}`);
    expect(fmtSignedMoney(-31.25)).toBe(`-$${digits(31.25)}`);
    expect(fmtSignedMoney(0)).toBe(`+$${digits(0)}`);

    // The sign belongs before the currency symbol. `fmtMoney` on a negative
    // produces the nonstandard "$-31.25", which is why P&L uses this instead.
    expect(fmtSignedMoney(-31.25)).not.toContain('$-');
    expect(fmtSignedMoney(-31.25).startsWith('-$')).toBe(true);
  });

  it('formats percentages with a leading + only when non-negative', () => {
    expect(fmtPct(1.234)).toBe('+1.23%');
    expect(fmtPct(-0.99)).toBe('-0.99%');
  });

  it('abbreviates volume by magnitude', () => {
    expect(fmtVolume(3_400_000)).toBe('3.4M');
    expect(fmtVolume(2_500)).toBe('2.5K');
    expect(fmtVolume(250)).toBe('250');
    expect(fmtVolume(4_100_000_000)).toBe('4.1B');
  });

  it('renders confidence as a whole percentage', () => {
    expect(fmtConfidence(0.71)).toBe('71%');
    expect(fmtConfidence(0)).toBe('0%');
    expect(fmtConfidence(1)).toBe('100%');
  });

  // A confidence outside 0..1 means the producer is wrong. Rendering "150%",
  // or silently clamping to "100%", would present that bug to the user as a
  // real reading.
  it('treats an out-of-range confidence as unavailable, not clamped', () => {
    expect(fmtConfidence(1.5)).toBe(DASH);
    expect(fmtConfidence(-0.2)).toBe(DASH);
    expect(fmtConfidence(100)).toBe(DASH);
    expect(fmtConfidence(1.0001)).toBe(DASH);

    expect(isUsableConfidence(1.5)).toBe(false);
    expect(isUsableConfidence(-0.2)).toBe(false);
    expect(isUsableConfidence(Number.NaN)).toBe(false);
    expect(isUsableConfidence(0.71)).toBe(true);
    expect(isUsableConfidence(0)).toBe(true);
    expect(isUsableConfidence(1)).toBe(true);
  });

  it('returns an em dash for an unparseable or absent timestamp', () => {
    expect(fmtTime('not-a-date')).toBe(DASH);
    expect(fmtTime('')).toBe(DASH);
    expect(fmtTime(undefined)).toBe(DASH);
  });

  it('parses a valid ISO timestamp', () => {
    expect(fmtTime('2026-09-07T14:31:00Z')).not.toBe(DASH);
  });
});

describe('changeColor', () => {
  it('falls back to the muted colour for unusable values', () => {
    expect(changeColor(Number.NaN, '#888')).toBe('#888');
    expect(changeColor(undefined, '#888')).toBe('#888');
  });

  it('is green at or above zero and red below', () => {
    expect(changeColor(0, '#888')).toBe('#10b981');
    expect(changeColor(-0.01, '#888')).toBe('#ef4444');
  });
});
