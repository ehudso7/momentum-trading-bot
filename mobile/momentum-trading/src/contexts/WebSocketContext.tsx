import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import Constants from 'expo-constants';

interface WebSocketContextType {
  isConnected: boolean;
  subscribe: (channel: string) => void;
  unsubscribe: (channel: string) => void;
  sendMessage: (message: any) => void;
  lastMessage: any;
}

const WebSocketContext = createContext<WebSocketContextType | undefined>(undefined);

const WS_URL = Constants.expoConfig?.extra?.apiUrl?.replace('https', 'wss') + '/ws' ||
               'wss://momentum-trading-bot-production.up.railway.app/ws';

export function WebSocketProvider({ children }: { children: React.ReactNode }) {
  const ws = useRef<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<any>(null);
  const reconnectTimeout = useRef<NodeJS.Timeout | null>(null);
  const subscribedChannels = useRef<Set<string>>(new Set());

  useEffect(() => {
    connect();
    return () => {
      disconnect();
    };
  }, []);

  const connect = () => {
    try {
      ws.current = new WebSocket(WS_URL);

      ws.current.onopen = () => {
        console.log('WebSocket connected');
        setIsConnected(true);

        // Resubscribe to channels after reconnection
        subscribedChannels.current.forEach(channel => {
          sendMessage({ type: 'subscribe', channel });
        });
      };

      ws.current.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setLastMessage(data);
        } catch (error) {
          console.error('Error parsing WebSocket message:', error);
        }
      };

      ws.current.onerror = (error) => {
        console.error('WebSocket error:', error);
      };

      ws.current.onclose = () => {
        console.log('WebSocket disconnected');
        setIsConnected(false);

        // Attempt to reconnect after 3 seconds
        reconnectTimeout.current = setTimeout(() => {
          connect();
        }, 3000);
      };
    } catch (error) {
      console.error('Error connecting to WebSocket:', error);
    }
  };

  const disconnect = () => {
    if (reconnectTimeout.current) {
      clearTimeout(reconnectTimeout.current);
    }

    if (ws.current) {
      ws.current.close();
      ws.current = null;
    }
  };

  const subscribe = (channel: string) => {
    subscribedChannels.current.add(channel);
    if (isConnected) {
      sendMessage({ type: 'subscribe', channel });
    }
  };

  const unsubscribe = (channel: string) => {
    subscribedChannels.current.delete(channel);
    if (isConnected) {
      sendMessage({ type: 'unsubscribe', channel });
    }
  };

  const sendMessage = (message: any) => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify(message));
    }
  };

  return (
    <WebSocketContext.Provider
      value={{ isConnected, subscribe, unsubscribe, sendMessage, lastMessage }}
    >
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocket() {
  const context = useContext(WebSocketContext);
  if (context === undefined) {
    throw new Error('useWebSocket must be used within a WebSocketProvider');
  }
  return context;
}