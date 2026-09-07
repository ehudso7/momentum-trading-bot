import React from 'react';
import { fireEvent, waitFor } from '@testing-library/react-native';
import SignalsScreen from '../SignalsScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';
import { fmtMoney, fmtSignedMoney, fmtPct } from '../../utils/format';

jest.mock('../../contexts/WebSocketContext', () => ({
  useWebSocket: () => ({ subscribe: jest.fn(), lastMessage: null, send: jest.fn() }),
}));

// Only the network client is mocked. Spreading requireActual keeps the real
// type guards and helpers the screen imports from this module — mocking the
// whole module replaced isHistoricalSignal with undefined, which threw during
// render and surfaced as "Unable to find node on an unmounted component".
jest.mock('../../services/api', () => ({
  ...jest.requireActual('../../services/api'),
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
    expect(screen.getByText(fmtMoney(4.32))).toBeOnTheScreen();
    expect(screen.getByText('71%')).toBeOnTheScreen();
    expect(screen.getByText('Held VWAP on the third test.')).toBeOnTheScreen();
  });

  // /signals/history returns entryPrice/exitPrice/profit/profitPercent/result
  // and carries neither `price` nor `reasoning`. An earlier revision typed
  // both endpoints with one shape, so history rows would have read undefined
  // from fields the endpoint never sends.
  it('renders history rows from the history payload', async () => {
    getLatestSignals.mockResolvedValue([]);
    getSignalHistory.mockResolvedValue([
      {
        id: 'hist-1',
        symbol: 'HIST',
        type: 'Support Bounce',
        action: 'BUY',
        confidence: 0.88,
        entryPrice: 380,
        exitPrice: 385.6,
        profit: 5.6,
        profitPercent: 1.47,
        result: 'win',
        timestamp: '2026-09-07T14:15:00Z',
      },
    ]);

    const screen = renderScreen(<SignalsScreen />);

    fireEvent.press(screen.getByText('History'));

    await waitFor(() => expect(screen.getByText('HIST')).toBeOnTheScreen());
    expect(screen.getByText(fmtMoney(380))).toBeOnTheScreen();
    expect(screen.getByText(fmtMoney(385.6))).toBeOnTheScreen();
    expect(
      screen.getByText(`${fmtSignedMoney(5.6)} (${fmtPct(1.47)})`),
    ).toBeOnTheScreen();

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('undefined');
    expect(text).not.toContain('NaN');
  });

  // A confidence outside 0..1 is a producer bug; showing "150%" would present
  // it to the user as a real reading.
  it('shows an em dash for an out-of-range confidence', async () => {
    getLatestSignals.mockResolvedValue([
      {
        id: 'sig-3',
        symbol: 'BADC',
        type: 'Breakout',
        action: 'BUY',
        confidence: 1.5,
        price: 10,
        timestamp: '2026-09-07T14:31:00Z',
      },
    ]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);

    await waitFor(() => expect(screen.getByText('BADC')).toBeOnTheScreen());
    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('150%');
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  // A signal with no action must not render a red badge implying a SELL, nor
  // an empty badge that reads as a broken UI.
  it('renders a missing action as an em dash, not an implied SELL', async () => {
    getLatestSignals.mockResolvedValue([
      {
        id: 'sig-4',
        symbol: 'NOACT',
        confidence: 0.6,
        price: 10,
        timestamp: '2026-09-07T14:31:00Z',
      },
    ]);
    getSignalHistory.mockResolvedValue([]);

    const screen = renderScreen(<SignalsScreen />);

    await waitFor(() => expect(screen.getByText('NOACT')).toBeOnTheScreen());
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('undefined');
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
