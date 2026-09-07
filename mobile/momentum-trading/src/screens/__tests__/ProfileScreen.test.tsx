import React from 'react';
import { waitFor } from '@testing-library/react-native';
import ProfileScreen from '../ProfileScreen';
import { renderScreen, renderedText, FABRICATED_LITERALS } from './renderScreen';

jest.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'u1', name: 'Test User', email: 't@example.invalid', tier: 'free' },
    logout: jest.fn(),
  }),
}));

jest.mock('../../services/api', () => ({
  api: {
    getSubscription: jest.fn(),
    getSettings: jest.fn(),
    createCheckoutSession: jest.fn(),
  },
}));

import { api } from '../../services/api';

describe('ProfileScreen', () => {
  beforeEach(() => {
    (api.getSubscription as jest.Mock).mockResolvedValue(null);
    (api.getSettings as jest.Mock).mockResolvedValue(null);
  });

  it('does not present an invented personal trading record', async () => {
    const screen = renderScreen(<ProfileScreen />);

    await waitFor(() =>
      expect(screen.getByText('Performance stats unavailable')).toBeOnTheScreen(),
    );

    const text = renderedText(screen.toJSON());
    for (const literal of FABRICATED_LITERALS) {
      expect(text).not.toContain(literal);
    }
    expect(text).not.toContain('Total Gains');
    expect(text).not.toContain('Trades Won');
    expect(text).not.toContain('Win Rate');
  });
});
