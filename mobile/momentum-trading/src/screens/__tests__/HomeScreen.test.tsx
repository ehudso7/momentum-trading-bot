import React from 'react';
import { waitFor } from '@testing-library/react-native';
import HomeScreen from '../HomeScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';
import { fmtMoney, fmtSignedMoney } from '../../utils/format';

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

  // Before this, an unresolved or failed signals query fell through to
  // "No active signals" — a claim about the account made while the app had no
  // idea. Loading and failure must be distinguishable from a real empty list.
  it('does not claim there are no signals while the query is still loading', async () => {
    getPortfolio.mockResolvedValue(null);
    getLatestSignals.mockReturnValue(new Promise(() => {})); // never settles

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() =>
      expect(screen.getByText('Loading signals…')).toBeOnTheScreen(),
    );
    expect(screen.queryByText('No active signals')).toBeNull();
  });

  it('does not claim there are no signals when the query fails', async () => {
    getPortfolio.mockResolvedValue(null);
    getLatestSignals.mockRejectedValue(new Error('unreachable'));

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() =>
      expect(screen.getByText('Signals unavailable')).toBeOnTheScreen(),
    );
    expect(screen.queryByText('No active signals')).toBeNull();
  });

  it('says there are no signals only when the API really returns none', async () => {
    getPortfolio.mockResolvedValue(null);
    getLatestSignals.mockResolvedValue([]);

    const screen = renderScreen(<HomeScreen />);

    await waitFor(() =>
      expect(screen.getByText('No active signals')).toBeOnTheScreen(),
    );
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
      expect(renderedText(screen.toJSON())).toContain(fmtMoney(4321.99)),
    );
    // The day's P&L carries an explicit sign rather than reading "$1.23".
    expect(screen.getByText(fmtSignedMoney(1.23))).toBeOnTheScreen();
  });
});
