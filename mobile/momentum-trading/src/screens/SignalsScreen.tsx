import React, { useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  RefreshControl,
  Alert,
} from 'react-native';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useTheme } from '../contexts/ThemeContext';
import { useWebSocket } from '../contexts/WebSocketContext';
import { api, Signal } from '../services/api';
import { fmtMoney, fmtConfidence, fmtTime } from '../utils/format';
import DataUnavailable from '../components/DataUnavailable';

type TabKey = 'latest' | 'history';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'latest', label: 'Latest' },
  { key: 'history', label: 'History' },
];

export default function SignalsScreen() {
  const { theme } = useTheme();
  const { subscribe } = useWebSocket();
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('latest');

  React.useEffect(() => {
    subscribe('signals:*');
  }, [subscribe]);

  const { data: signals, isLoading: signalsLoading, isError: signalsError } = useQuery({
    queryKey: ['signals', 'latest'],
    queryFn: api.getLatestSignals,
  });

  const {
    data: signalHistory,
    isLoading: historyLoading,
    isError: historyError,
  } = useQuery({
    queryKey: ['signals', 'history'],
    queryFn: api.getSignalHistory,
  });

  const subscribeToSignalMutation = useMutation({
    mutationFn: api.subscribeToSignal,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['signals'] });
      Alert.alert('Success', 'Subscribed to signal notifications');
    },
    onError: (error: any) => {
      Alert.alert('Error', error.message || 'Failed to subscribe to signal');
    },
  });

  const onRefresh = React.useCallback(async () => {
    setRefreshing(true);
    queryClient.invalidateQueries({ queryKey: ['signals'] });
    setTimeout(() => setRefreshing(false), 2000);
  }, [queryClient]);

  // The two tabs map one-to-one onto the two endpoints the SDK exposes. There
  // is deliberately no client-side "active/triggered" split: the backend does
  // not send a lifecycle status, and inventing one would put a made-up state
  // next to a real symbol.
  const visibleSignals: Signal[] =
    (activeTab === 'latest' ? signals : signalHistory) ?? [];
  const loading = activeTab === 'latest' ? signalsLoading : historyLoading;
  const errored = activeTab === 'latest' ? signalsError : historyError;

  const getSignalColor = (action: string) =>
    action?.toUpperCase() === 'BUY' ? '#10b981' : '#ef4444';

  const getConfidenceColor = (confidence: number) => {
    if (confidence >= 0.9) return '#10b981';
    if (confidence >= 0.8) return '#f59e0b';
    return '#ef4444';
  };

  const handleSubscribeToSignal = (signalId: string) => {
    subscribeToSignalMutation.mutate(signalId);
  };

  return (
    <SafeAreaView style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} />
        }
        showsVerticalScrollIndicator={false}
      >
        {/* Header */}
        <View style={styles.header}>
          <Text style={[styles.title, { color: theme.text }]}>Trading Signals</Text>
          <View style={styles.headerRight}>
            <TouchableOpacity style={styles.filterButton}>
              <Text style={[styles.filterIcon, { color: theme.textSecondary }]}>⚙️</Text>
            </TouchableOpacity>
          </View>
        </View>

        <Text style={[styles.disclosure, { color: theme.textSecondary }]}>
          Signals are research output, not investment advice, and this app does not
          place orders. Past signal results are not shown because no verified
          performance record has been published for them.
        </Text>

        {/* Tabs */}
        <View style={styles.tabs}>
          {TABS.map((tab) => (
            <TouchableOpacity
              key={tab.key}
              onPress={() => setActiveTab(tab.key)}
              style={[
                styles.tab,
                {
                  backgroundColor: activeTab === tab.key ? theme.primary : 'transparent',
                },
              ]}
            >
              <Text
                style={[
                  styles.tabText,
                  { color: activeTab === tab.key ? '#fff' : theme.textSecondary },
                ]}
              >
                {tab.label}
              </Text>
            </TouchableOpacity>
          ))}
        </View>

        {/* Signals List */}
        <View style={styles.signalsList}>
          {visibleSignals.length ? (
            visibleSignals.map((signal) => (
              <TouchableOpacity
                key={signal.id}
                style={[styles.signalCard, { backgroundColor: theme.card }]}
              >
                <View style={styles.signalHeader}>
                  <View style={styles.signalLeft}>
                    <Text style={[styles.signalSymbol, { color: theme.text }]}>
                      {signal.symbol}
                    </Text>
                    <Text style={[styles.signalType, { color: theme.textSecondary }]}>
                      {signal.type}
                    </Text>
                  </View>

                  <View style={styles.signalRight}>
                    <View
                      style={[
                        styles.actionBadge,
                        { backgroundColor: getSignalColor(signal.action) },
                      ]}
                    >
                      <Text style={styles.actionText}>{signal.action}</Text>
                    </View>
                    <Text style={[styles.signalTime, { color: theme.textSecondary }]}>
                      {fmtTime(signal.timestamp)}
                    </Text>
                  </View>
                </View>

                <View style={styles.signalBody}>
                  <View style={styles.priceInfo}>
                    <Text style={[styles.priceLabel, { color: theme.textSecondary }]}>
                      Reference Price
                    </Text>
                    <Text style={[styles.priceValue, { color: theme.text }]}>
                      {fmtMoney(signal.price)}
                    </Text>
                  </View>
                </View>

                <View style={styles.signalFooter}>
                  <View style={styles.confidenceContainer}>
                    <Text style={[styles.confidenceLabel, { color: theme.textSecondary }]}>
                      Signal Confidence
                    </Text>
                    <View style={styles.confidenceBar}>
                      <View
                        style={[
                          styles.confidenceFill,
                          {
                            width: `${Math.max(
                              0,
                              Math.min(1, signal.confidence ?? 0),
                            ) * 100}%`,
                            backgroundColor: getConfidenceColor(signal.confidence ?? 0),
                          },
                        ]}
                      />
                    </View>
                    <Text
                      style={[
                        styles.confidenceText,
                        { color: getConfidenceColor(signal.confidence ?? 0) },
                      ]}
                    >
                      {fmtConfidence(signal.confidence)}
                    </Text>
                  </View>

                  {signal.reasoning ? (
                    <View style={styles.reasoningContainer}>
                      <Text style={[styles.reasoningLabel, { color: theme.textSecondary }]}>
                        Reasoning:
                      </Text>
                      <Text style={[styles.reasoningText, { color: theme.text }]}>
                        {signal.reasoning}
                      </Text>
                    </View>
                  ) : null}

                  <View style={styles.signalActions}>
                    <TouchableOpacity
                      style={[
                        styles.actionButton,
                        {
                          backgroundColor: 'transparent',
                          borderWidth: 1,
                          borderColor: theme.border,
                        },
                      ]}
                      onPress={() => handleSubscribeToSignal(signal.id)}
                      disabled={subscribeToSignalMutation.isPending}
                    >
                      <Text style={[styles.actionButtonText, { color: theme.primary }]}>
                        Subscribe to alerts
                      </Text>
                    </TouchableOpacity>
                  </View>
                </View>
              </TouchableOpacity>
            ))
          ) : (
            <DataUnavailable
              title={loading ? 'Loading signals…' : 'No signals available'}
              detail={
                loading
                  ? undefined
                  : errored
                    ? 'The signals service could not be reached.'
                    : 'No signals have been published for this view.'
              }
            />
          )}
        </View>

        <View style={{ height: 100 }} />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: 20,
    paddingBottom: 10,
  },
  title: {
    fontSize: 28,
    fontWeight: 'bold',
  },
  headerRight: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  filterButton: {
    padding: 8,
  },
  filterIcon: {
    fontSize: 20,
  },
  disclosure: {
    marginHorizontal: 20,
    marginBottom: 16,
    fontSize: 12,
    lineHeight: 17,
  },
  tabs: {
    flexDirection: 'row',
    marginHorizontal: 20,
    marginBottom: 20,
    backgroundColor: 'rgba(102, 126, 234, 0.1)',
    borderRadius: 12,
    padding: 4,
  },
  tab: {
    flex: 1,
    paddingVertical: 8,
    alignItems: 'center',
    borderRadius: 8,
  },
  tabText: {
    fontSize: 14,
    fontWeight: '500',
  },
  signalsList: {
    paddingHorizontal: 20,
  },
  signalCard: {
    marginBottom: 16,
    padding: 20,
    borderRadius: 16,
    elevation: 2,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.22,
    shadowRadius: 2.22,
  },
  signalHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 16,
  },
  signalLeft: {
    flex: 1,
  },
  signalSymbol: {
    fontSize: 20,
    fontWeight: 'bold',
    marginBottom: 4,
  },
  signalType: {
    fontSize: 14,
  },
  signalRight: {
    alignItems: 'flex-end',
  },
  actionBadge: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 12,
    marginBottom: 4,
  },
  actionText: {
    color: '#fff',
    fontSize: 12,
    fontWeight: 'bold',
  },
  signalTime: {
    fontSize: 12,
  },
  signalBody: {
    marginBottom: 16,
  },
  priceInfo: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  priceLabel: {
    fontSize: 14,
  },
  priceValue: {
    fontSize: 16,
    fontWeight: '600',
  },
  signalFooter: {
    borderTopWidth: 1,
    borderTopColor: 'rgba(102, 126, 234, 0.1)',
    paddingTop: 16,
  },
  confidenceContainer: {
    marginBottom: 12,
  },
  confidenceLabel: {
    fontSize: 12,
    marginBottom: 8,
  },
  confidenceBar: {
    height: 4,
    backgroundColor: 'rgba(102, 126, 234, 0.2)',
    borderRadius: 2,
    marginBottom: 4,
  },
  confidenceFill: {
    height: '100%',
    borderRadius: 2,
  },
  confidenceText: {
    fontSize: 12,
    fontWeight: '600',
    textAlign: 'right',
  },
  reasoningContainer: {
    marginBottom: 16,
  },
  reasoningLabel: {
    fontSize: 12,
    marginBottom: 4,
  },
  reasoningText: {
    fontSize: 13,
    lineHeight: 18,
  },
  signalActions: {
    flexDirection: 'row',
    gap: 8,
  },
  actionButton: {
    flex: 1,
    paddingVertical: 10,
    borderRadius: 8,
    alignItems: 'center',
  },
  actionButtonText: {
    color: '#fff',
    fontSize: 14,
    fontWeight: '600',
  },
});
