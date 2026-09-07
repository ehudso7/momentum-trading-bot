import React from 'react';
import { waitFor } from '@testing-library/react-native';
import TradingScreen from '../TradingScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';

jest.mock('../../contexts/WebSocketContext', () => ({
  useWebSocket: () => ({ subscribe: jest.fn(), lastMessage: null, send: jest.fn() }),
}));

jest.mock('../../services/api', () => ({
  api: { getMarketData: jest.fn() },
}));

import { api } from '../../services/api';

const getMarketData = api.getMarketData as jest.Mock;

describe('TradingScreen', () => {
  beforeEach(() => getMarketData.mockReset());

  it('renders em dashes rather than a sample quote when the API fails', async () => {
    getMarketData.mockRejectedValue(new Error('unreachable'));

    const screen = renderScreen(<TradingScreen />);

    await waitFor(() =>
      expect(screen.getByText('Quote unavailable')).toBeOnTheScreen(),
    );

    const text = renderedText(screen.toJSON());
    for (const literal of FABRICATED_LITERALS) {
      expect(text).not.toContain(literal);
    }
  });

  it('renders the quote returned for the selected symbol', async () => {
    getMarketData.mockResolvedValue({
      symbol: 'AAPL',
      price: 12.34,
      change: -0.56,
      changePercent: -4.34,
      volume: 3_400_000,
      open: 12.9,
      high: 13.1,
      low: 12.2,
      previousClose: 12.9,
      timestamp: '2026-09-07T14:31:00Z',
    });

    const screen = renderScreen(<TradingScreen />);

    await waitFor(() => expect(screen.getByText('$12.34')).toBeOnTheScreen());
    expect(screen.getByText('-$0.56 (-4.34%)')).toBeOnTheScreen();
    expect(screen.getByText('$12.90')).toBeOnTheScreen();
    expect(screen.getByText('3.4M')).toBeOnTheScreen();
  });

  it('does not plot a price series while none is published', async () => {
    getMarketData.mockResolvedValue({
      symbol: 'AAPL',
      price: 12.34,
      change: 0,
      changePercent: 0,
      volume: 1,
      open: 1,
      high: 1,
      low: 1,
      previousClose: 1,
      timestamp: '2026-09-07T14:31:00Z',
    });

    const screen = renderScreen(<TradingScreen />);

    await waitFor(() =>
      expect(screen.getByText('Price history unavailable')).toBeOnTheScreen(),
    );
    expect(screen.queryByTestId('line-chart')).toBeNull();
  });

  it('keeps the non-advice disclaimer and does not offer in-app order placement', async () => {
    getMarketData.mockResolvedValue(undefined);

    const screen = renderScreen(<TradingScreen />);

    await waitFor(() =>
      expect(
        screen.getByText('Educational information, not personalized advice'),
      ).toBeOnTheScreen(),
    );

    const text = renderedText(screen.toJSON());
    expect(text).not.toContain('Place Order');
    expect(text).not.toContain('Buy Now');
    expect(text).toContain('Momentum does not route or execute orders.');
  });
});
