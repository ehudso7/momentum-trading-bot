import React from 'react';
import { waitFor } from '@testing-library/react-native';
import PortfolioScreen from '../PortfolioScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';

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
    getPerformance.mockResolvedValue(null);

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
    getPerformance.mockResolvedValue(null);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('WXYZ')).toBeOnTheScreen());
    expect(screen.getByText('50 shares')).toBeOnTheScreen();
    expect(screen.getByText('$150.25')).toBeOnTheScreen();
    expect(screen.getByText('+$100.00')).toBeOnTheScreen();
    expect(screen.getByText('$1,234.56')).toBeOnTheScreen();
    expect(screen.getByText('-0.99%')).toBeOnTheScreen();
  });

  it('renders an em dash rather than NaN when a position omits its numbers', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: Number.NaN,
      dayChange: Number.POSITIVE_INFINITY,
      dayChangePercent: Number.NaN,
      positions: [],
    });
    getPositions.mockResolvedValue([{ symbol: 'NOPE' }]);
    getPerformance.mockResolvedValue(null);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('NOPE')).toBeOnTheScreen());

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('Infinity');
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
