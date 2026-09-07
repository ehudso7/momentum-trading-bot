import React from 'react';
import { waitFor } from '@testing-library/react-native';
import PortfolioScreen from '../PortfolioScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';
// Compared against the real formatters, not hardcoded strings: fmtMoney uses
// the runtime locale, so '$1,234.56' would fail on a de-DE runner. Formatter
// behaviour itself is pinned in src/utils/__tests__/format.test.ts.
import { fmtMoney, fmtSignedMoney } from '../../utils/format';

jest.mock('../../services/api', () => ({
  api: {
    getPortfolio: jest.fn(),
    getPositions: jest.fn(),
    getPerformance: jest.fn(),
  },
}));

import { api } from '../../services/api';

const getPortfolio = api.getPortfolio as jest.Mock;
const getPositions = api.getPositions as jest.Mock;
const getPerformance = api.getPerformance as jest.Mock;

describe('PortfolioScreen', () => {
  beforeEach(() => {
    getPortfolio.mockReset();
    getPositions.mockReset();
    getPerformance.mockReset();
  });

  it('shows no invented holdings or equity when the API returns an empty portfolio', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockResolvedValue([]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getAllByText('—').length).toBeGreaterThan(0));

    const text = renderedText(screen.toJSON());
    for (const literal of FABRICATED_LITERALS) {
      expect(text).not.toContain(literal);
    }
    expect(text).not.toContain('AAPL');
    expect(text).not.toContain('TSLA');
    expect(screen.queryByTestId('pie-chart')).toBeNull();
    expect(screen.queryByTestId('line-chart')).toBeNull();
  });

  // The payload below is the shape trading_bot/api/mobile_routes.py actually
  // returns from GET /positions. An earlier version of this test invented
  // `shares`/`value`/`change` keys, matching the old TypeScript type rather
  // than the server, so it passed while the screen called
  // `position.value.toLocaleString()` on a field the API never sends.
  it('renders positions in the shape the backend actually returns', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: 1234.56,
      dayChange: -12.34,
      dayChangePercent: -0.99,
      positions: [],
    });
    getPositions.mockResolvedValue([
      {
        symbol: 'WXYZ',
        quantity: 50,
        side: 'long',
        entryPrice: 148.25,
        currentPrice: 150.25,
        unrealizedPnL: 100,
        entryDate: '2026-09-07T10:30:00Z',
      },
    ]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('WXYZ')).toBeOnTheScreen());
    expect(screen.getByText('50 shares')).toBeOnTheScreen();
    expect(screen.getByText(fmtMoney(150.25))).toBeOnTheScreen();
    expect(screen.getByText(fmtSignedMoney(100))).toBeOnTheScreen();
    expect(screen.getByText(fmtMoney(1234.56))).toBeOnTheScreen();
    expect(screen.getByText('-0.99%')).toBeOnTheScreen();
  });

  // A losing day previously rendered "$-12.34" because the summary used
  // fmtMoney. The sign belongs before the currency symbol.
  // The /performance query used to run here (and re-run on every period
  // change) while its result was never rendered — the chart is hardcoded
  // unavailable. The request, and the period selector that triggered it, are
  // gone until a charted series exists.
  it('does not call the performance endpoint while no series is rendered', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockResolvedValue([]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() =>
      expect(screen.getByText('Performance history unavailable')).toBeOnTheScreen(),
    );
    expect(getPerformance).not.toHaveBeenCalled();
    expect(screen.queryByText('1D')).toBeNull();
    expect(screen.queryByText('ALL')).toBeNull();
  });

  // An absent entry price used to render an empty string, leaving a blank
  // line in the row rather than saying the value is unavailable.
  it('renders an explicit entry price even when the field is absent', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockResolvedValue([{ symbol: 'NOENTRY', quantity: 1 }]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('NOENTRY')).toBeOnTheScreen());
    expect(screen.getByText(`entry ${fmtMoney(undefined)}`)).toBeOnTheScreen();
  });

  it('does not claim there are no holdings while the query is still loading', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockReturnValue(new Promise(() => {})); // never settles

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() =>
      expect(screen.getByText('Loading holdings…')).toBeOnTheScreen(),
    );
    expect(screen.queryByText('No holdings to display')).toBeNull();
  });

  it('does not claim there are no holdings when the query fails', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockRejectedValue(new Error('unreachable'));

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() =>
      expect(screen.getByText('Holdings unavailable')).toBeOnTheScreen(),
    );
    expect(screen.queryByText('No holdings to display')).toBeNull();
  });

  // The centre column is styled as a position "value" but shows a per-share
  // price, so it must say which it is.
  it('labels the per-share price rather than implying position value', async () => {
    getPortfolio.mockResolvedValue(null);
    getPositions.mockResolvedValue([
      { symbol: 'LBL', quantity: 3, side: 'long', currentPrice: 10 },
    ]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('LBL')).toBeOnTheScreen());
    expect(screen.getByText('price · long')).toBeOnTheScreen();
    // 3 x 10 = 30 would be the market value; it is deliberately not shown.
    expect(renderedText(screen.toJSON())).not.toContain('$30.00');
  });

  it('renders a losing day as -$… and never $-…', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: 1000,
      dayChange: -12.34,
      dayChangePercent: -1.22,
      positions: [],
    });
    getPositions.mockResolvedValue([]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() =>
      expect(screen.getByText(fmtSignedMoney(-12.34))).toBeOnTheScreen(),
    );
    expect(renderedText(screen.toJSON())).not.toContain('$-');
  });

  it('renders an em dash rather than NaN when a position omits its numbers', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: Number.NaN,
      dayChange: Number.POSITIVE_INFINITY,
      dayChangePercent: Number.NaN,
      positions: [],
    });
    getPositions.mockResolvedValue([{ symbol: 'NOPE' }]);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('NOPE')).toBeOnTheScreen());

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('Infinity');
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
