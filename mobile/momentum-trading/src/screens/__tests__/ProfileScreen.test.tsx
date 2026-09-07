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

const getSubscription = api.getSubscription as jest.Mock;
const getSettings = api.getSettings as jest.Mock;

describe('ProfileScreen', () => {
  beforeEach(() => {
    getSubscription.mockReset().mockResolvedValue(null);
    getSettings.mockReset().mockResolvedValue(null);
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

  // The /subscription and /settings queries were removed because nothing
  // rendered their results — a request firing on every mount for data the UI
  // never shows. PortfolioScreen's equivalent /performance removal already
  // has this guard; without the same one here, a refactor could reintroduce
  // the dead calls silently.
  it('issues no request whose result nothing renders', async () => {
    const screen = renderScreen(<ProfileScreen />);

    await waitFor(() =>
      expect(screen.getByText('Performance stats unavailable')).toBeOnTheScreen(),
    );

    expect(getSubscription).not.toHaveBeenCalled();
    expect(getSettings).not.toHaveBeenCalled();
  });
});
