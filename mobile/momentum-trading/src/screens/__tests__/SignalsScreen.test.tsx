import React from 'react';
import { waitFor } from '@testing-library/react-native';
import SignalsScreen from '../SignalsScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';

jest.mock('../../contexts/WebSocketContext', () => ({
  useWebSocket: () => ({ subscribe: jest.fn(), lastMessage: null, send: jest.fn() }),
}));

jest.mock('../../services/api', () => ({
  api: {
    getLatestSignals: jest.fn(),
    getSignalHistory: jest.fn(),
    subscribeToSignal: jest.fn(),
  },
}));

import { api } from '../../services/api';

const getLatestSignals = api.getLatestSignals as jest.Mock;
const getSignalHistory = api.getSignalHistory as jest.Mock;

describe('SignalsScreen', () => {
  beforeEach(() => {
    getLatestSignals.mockReset();
    getSignalHistory.mockReset();
  });

  it('shows an explicit unavailable state instead of sample signals', async () => {
    getLatestSignals.mockResolvedValue([]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);

    await waitFor(() =>
      expect(screen.getByText('No signals available')).toBeOnTheScreen(),
    );

    const text = renderedText(screen.toJSON());
    for (const literal of FABRICATED_LITERALS) {
      expect(text).not.toContain(literal);
    }
  });

  it('renders signals returned by the API rather than fixtures', async () => {
    getLatestSignals.mockResolvedValue([
      {
        id: 'sig-1',
        symbol: 'ZZZZ',
        type: 'VWAP pullback',
        action: 'BUY',
        confidence: 0.71,
        price: 4.32,
        timestamp: '2026-09-07T14:31:00Z',
        reasoning: 'Held VWAP on the third test.',
      },
    ]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);

    await waitFor(() => expect(screen.getByText('ZZZZ')).toBeOnTheScreen());
    expect(screen.getByText('VWAP pullback')).toBeOnTheScreen();
    expect(screen.getByText('$4.32')).toBeOnTheScreen();
    expect(screen.getByText('71%')).toBeOnTheScreen();
    expect(screen.getByText('Held VWAP on the third test.')).toBeOnTheScreen();
  });

  it('does not claim a performance record', async () => {
    getLatestSignals.mockResolvedValue([]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);
    await waitFor(() =>
      expect(screen.getByText('No signals available')).toBeOnTheScreen(),
    );

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('Accuracy');
    expect(text).not.toContain('Avg Return');
    expect(text).not.toContain('Market Regime Detection');
  });

  it('renders an em dash when a signal omits numeric fields', async () => {
    getLatestSignals.mockResolvedValue([
      {
        id: 'sig-2',
        symbol: 'QQQQ',
        type: 'Gap fill',
        action: 'SELL',
        timestamp: 'not-a-date',
      },
    ]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);
    await waitFor(() => expect(screen.getByText('QQQQ')).toBeOnTheScreen());
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
