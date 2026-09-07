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

describe('formatting of real values', () => {
  it('formats money to two decimals with separators', () => {
    expect(fmtMoney(1234.5)).toBe('$1,234.50');
    expect(fmtMoney(0)).toBe('$0.00');
  });

  it('always shows an explicit sign for signed money', () => {
    expect(fmtSignedMoney(100)).toBe('+$100.00');
    expect(fmtSignedMoney(-31.25)).toBe('-$31.25');
    expect(fmtSignedMoney(0)).toBe('+$0.00');
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
