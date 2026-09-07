/**
 * Display formatters for financial values.
 *
 * These live in one place on purpose. Each screen previously carried its own
 * copy, and two of them drifted: HomeScreen and PortfolioScreen checked only
 * `typeof n === 'number'`, which is true for NaN and Infinity, so a corrupt
 * or arithmetic-derived value would render as "$NaN" or "$Infinity" instead
 * of the em dash the rest of the app uses. Rendering a junk number where a
 * user reads their own money is the same class of defect as rendering a made
 * up one, so every formatter here treats non-finite as unavailable.
 *
 * Add new value formatters here rather than in a screen.
 */

/** Shown wherever a real value is unavailable. */
export const DASH = '—';

const isRenderableNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/** `$1,234.56`, or an em dash when the value is missing or non-finite. */
export function fmtMoney(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** `+$12.34` / `-$12.34`, sign always explicit. */
export function fmtSignedMoney(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  const sign = value >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** `+1.23%` / `-1.23%`. */
export function fmtPct(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

/** `3.4M`, `12.0K`, `250`. */
export function fmtVolume(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

/** A plain count, e.g. a share quantity. */
export function fmtQuantity(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

/**
 * `71%` from a 0..1 confidence.
 *
 * Values outside 0..1 are unavailable, not clamped. A confidence of 1.5 means
 * the producer is wrong, and "150%" — or a silently clamped "100%" — would
 * present that bug to the user as a real reading. An em dash says the value
 * could not be trusted, which is the honest rendering.
 */
export function fmtConfidence(value: number | null | undefined): string {
  if (!isRenderableNumber(value)) return DASH;
  if (value < 0 || value > 1) return DASH;
  return `${(value * 100).toFixed(0)}%`;
}

/** True when a confidence is usable for the 0..100% progress bar. */
export function isUsableConfidence(value: number | null | undefined): boolean {
  return isRenderableNumber(value) && value >= 0 && value <= 1;
}

/**
 * An optional text field, or an em dash when absent or blank.
 *
 * Rendering `{signal.action}` directly gives an empty element when the field
 * is missing, which reads as a broken UI rather than as missing data. Blank
 * strings count as absent for the same reason.
 */
export function fmtText(value: string | null | undefined): string {
  if (typeof value !== 'string') return DASH;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : DASH;
}

/** Local `HH:MM` from an ISO timestamp, or an em dash if unparseable. */
export function fmtTime(timestamp: string | null | undefined): string {
  if (!timestamp) return DASH;
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return DASH;
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * Colour for a trade action. Only an explicit BUY or SELL gets a colour; an
 * absent or unrecognised action is unknown, and colouring it red would imply
 * a SELL the payload never contained.
 */
export function actionColor(
  action: string | null | undefined,
  muted: string,
): string {
  const normalised = typeof action === 'string' ? action.trim().toUpperCase() : '';
  if (normalised === 'BUY') return '#10b981';
  if (normalised === 'SELL') return '#ef4444';
  return muted;
}

/** Colour helper: green when non-negative, red when negative, muted otherwise. */
export function changeColor(
  value: number | null | undefined,
  muted: string,
): string {
  if (!isRenderableNumber(value)) return muted;
  return value >= 0 ? '#10b981' : '#ef4444';
}
