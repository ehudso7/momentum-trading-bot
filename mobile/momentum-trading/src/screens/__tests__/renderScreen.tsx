import React from 'react';
import { render } from '@testing-library/react-native';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ThemeProvider } from '../../contexts/ThemeContext';

/**
 * Renders a screen with the providers it needs and nothing else stubbed.
 *
 * The real ThemeProvider is used so styling code paths execute; only the
 * transport-level contexts (WebSocket, Auth) and the API client are mocked by
 * individual test files, because those reach the network.
 */
export function renderScreen(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  );
}

/**
 * Literals that previously shipped as hardcoded UI values. Any of these
 * reappearing in rendered output means fabricated financial data has been
 * reintroduced. Keep this list append-only.
 */
export const FABRICATED_LITERALS = [
  '112,450.73',
  '105,000',
  '12,450',
  '150.25',
  '148.25',
  '152.10',
  '147.80',
  '95.2%',
  '+24.8%',
  '87%',
  '156',
  '2.5M',
  'Apple Inc.',
  'Momentum Breakout',
  'Reversal Pattern',
  'Quantum predictor',
];

/** Collects every string rendered anywhere in the tree. */
export function renderedText(json: any): string {
  const out: string[] = [];
  const walk = (node: any) => {
    if (node == null) return;
    if (typeof node === 'string' || typeof node === 'number') {
      out.push(String(node));
      return;
    }
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (node.children) walk(node.children);
  };
  walk(json);
  return out.join(' | ');
}
