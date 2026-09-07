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

  it('shows no invented holdings or equity when the API returns nothing', async () => {
    getPortfolio.mockResolvedValue(undefined);
    getPositions.mockResolvedValue([]);
    getPerformance.mockResolvedValue(undefined);

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

  it('renders positions returned by the API', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: 1234.56,
      dayChange: -12.34,
      dayChangePercent: -0.99,
      positions: [],
    });
    getPositions.mockResolvedValue([
      {
        symbol: 'WXYZ',
        name: 'Test Holding',
        shares: 10,
        value: 500,
        change: 5,
        changePercent: 1.01,
      },
    ]);
    getPerformance.mockResolvedValue(undefined);

    const screen = renderScreen(<PortfolioScreen />);

    await waitFor(() => expect(screen.getByText('WXYZ')).toBeOnTheScreen());
  });
});
