import type { WebSocketContextType } from '../../contexts/WebSocketContext';

/**
 * A WebSocket context mock that is checked against the real contract.
 *
 * The screen tests previously each hand-wrote `{ subscribe, lastMessage, send }`.
 * `send` does not exist — the real hook exposes `sendMessage` — and
 * `isConnected` and `unsubscribe` were missing entirely. A screen calling
 * `sendMessage` would have hit undefined while the tests stayed green, and a
 * screen calling the invented `send` would have passed in tests and crashed in
 * the app.
 *
 * The return type is the real `WebSocketContextType`, so any drift between
 * this mock and the context is a typecheck failure rather than a silent
 * false green.
 */
export function makeWebSocketMock(
  overrides: Partial<WebSocketContextType> = {},
): WebSocketContextType {
  return {
    isConnected: true,
    subscribe: jest.fn(),
    unsubscribe: jest.fn(),
    sendMessage: jest.fn(),
    lastMessage: null,
    ...overrides,
  };
}
