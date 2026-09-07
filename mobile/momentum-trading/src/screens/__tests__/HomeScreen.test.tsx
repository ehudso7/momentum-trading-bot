import React from 'react';
import { waitFor } from '@testing-library/react-native';
import HomeScreen from '../HomeScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';

jest.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'u1', name: 'Test User', email: 't@example.invalid', tier: 'free' },
    logout: jest.fn(),
  }),
}));

jest.mock('../../contexts/WebSocketContext', () => ({
  useWebSocket: () => ({ subscribe: jest.fn(), lastMessage: null, send: jest.fn() }),
}));

jest.mock('../../services/api', () => ({
  api: { getPortfolio: jest.fn(), getLatestSignals: jest.fn() },
}));

import { api } from '../../services/api';

const getPortfolio = api.getPortfolio as jest.Mock;
const getLatestSignals = api.getLatestSignals as jest.Mock;

describe('HomeScreen', () => {
  beforeEach(() => {
    getPortfolio.mockReset();
    getLatestSignals.mockReset();
  });

  it('never falls back to a sample balance or equity curve', async () => {
    getPortfolio.mockResolvedValue(null);
    getLatestSignals.mockResolvedValue([]);

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() => expect(screen.getAllByText('—').length).toBeGreaterThan(0));

    const text = renderedText(screen.toJSON());
    for (const literal of FABRICATED_LITERALS) {
      expect(text).not.toContain(literal);
    }
    expect(screen.queryByTestId('line-chart')).toBeNull();
  });

  it('renders an em dash rather than NaN or Infinity', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: Number.NaN,
      dayChange: Number.POSITIVE_INFINITY,
      dayChangePercent: Number.NaN,
      positions: [],
    });
    getLatestSignals.mockResolvedValue([]);

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() => expect(screen.getAllByText('—').length).toBeGreaterThan(0));
    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('Infinity');
  });

  it('renders the portfolio value the API returns', async () => {
    getPortfolio.mockResolvedValue({
      totalValue: 4321.99,
      dayChange: 1.23,
      dayChangePercent: 0.03,
      positions: [],
    });
    getLatestSignals.mockResolvedValue([]);

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() =>
      expect(renderedText(screen.toJSON())).toContain('4,321.99'),
    );
  });
});
