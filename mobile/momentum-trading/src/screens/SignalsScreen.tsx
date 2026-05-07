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
import { LinearGradient } from 'expo-linear-gradient';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useTheme } from '../contexts/ThemeContext';
import { useWebSocket } from '../contexts/WebSocketContext';
import { api } from '../services/api';

export default function SignalsScreen() {
  const { theme } = useTheme();
  const { subscribe, lastMessage } = useWebSocket();
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState('all');

  React.useEffect(() => {
    subscribe('signals:*');
  }, [subscribe]);

  const { data: signals } = useQuery({
    queryKey: ['signals', 'latest'],
    queryFn: api.getLatestSignals,
  });

  const { data: signalHistory } = useQuery({
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

  const mockSignals = [
    {
      id: '1',
      symbol: 'AAPL',
      type: 'Momentum Breakout',
      action: 'BUY',
      confidence: 0.95,
      price: 150.25,
      stopLoss: 147.50,
      takeProfit: [155.00, 158.50, 162.00],
      timestamp: new Date(),
      status: 'active',
      aiReasoning: 'Strong momentum with volume confirmation above 20-day moving average',
      riskReward: 2.8,
    },
    {
      id: '2',
      symbol: 'TSLA',
      type: 'Reversal Pattern',
      action: 'SELL',
      confidence: 0.87,
      price: 250.75,
      stopLoss: 255.00,
      takeProfit: [245.00, 240.00, 235.00],
      timestamp: new Date(Date.now() - 30 * 60 * 1000),
      status: 'active',
      aiReasoning: 'Double top pattern with RSI divergence indicating potential reversal',
      riskReward: 3.2,
    },
    {
      id: '3',
      symbol: 'NVDA',
      type: 'AI Volatility',
      action: 'BUY',
      confidence: 0.92,
      price: 520.50,
      stopLoss: 510.00,
      takeProfit: [535.00, 550.00, 565.00],
      timestamp: new Date(Date.now() - 60 * 60 * 1000),
      status: 'triggered',
      aiReasoning: 'Quantum predictor detected 95% probability of upward movement',
      riskReward: 2.1,
    },
  ];

  const getSignalColor = (action: string) => {
    return action === 'BUY' ? '#10b981' : '#ef4444';
  };

  const getConfidenceColor = (confidence: number) => {
    if (confidence >= 0.9) return '#10b981';
    if (confidence >= 0.8) return '#f59e0b';
    return '#ef4444';
  };

  const formatTime = (date: Date) => {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
          <Text style={[styles.title, { color: theme.text }]}>AI Trading Signals</Text>
          <View style={styles.headerRight}>
            <TouchableOpacity style={styles.filterButton}>
              <Text style={[styles.filterIcon, { color: theme.textSecondary }]}>⚙️</Text>
            </TouchableOpacity>
          </View>
        </View>

        {/* Performance Summary */}
        <LinearGradient
          colors={['#667eea', '#764ba2']}
          style={styles.performanceCard}
        >
          <Text style={styles.performanceTitle}>AI Signal Performance</Text>

          <View style={styles.performanceStats}>
            <View style={styles.performanceStat}>
              <Text style={styles.performanceValue}>95.2%</Text>
              <Text style={styles.performanceLabel}>Accuracy</Text>
            </View>
            <View style={styles.performanceStat}>
              <Text style={styles.performanceValue}>+24.8%</Text>
              <Text style={styles.performanceLabel}>Avg Return</Text>
            </View>
            <View style={styles.performanceStat}>
              <Text style={styles.performanceValue}>2.8:1</Text>
              <Text style={styles.performanceLabel}>Risk/Reward</Text>
            </View>
          </View>
        </LinearGradient>

        {/* Tabs */}
        <View style={styles.tabs}>
          {['all', 'active', 'history'].map((tab) => (
            <TouchableOpacity
              key={tab}
              onPress={() => setActiveTab(tab)}
              style={[
                styles.tab,
                {
                  backgroundColor: activeTab === tab ? theme.primary : 'transparent',
                },
              ]}
            >
              <Text
                style={[
                  styles.tabText,
                  { color: activeTab === tab ? '#fff' : theme.textSecondary },
                ]}
              >
                {tab.charAt(0).toUpperCase() + tab.slice(1)}
              </Text>
            </TouchableOpacity>
          ))}
        </View>

        {/* Signals List */}
        <View style={styles.signalsList}>
          {mockSignals
            .filter((signal) =>
              activeTab === 'all' ||
              (activeTab === 'active' && signal.status === 'active') ||
              (activeTab === 'history' && signal.status === 'triggered')
            )
            .map((signal) => (
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
                      {formatTime(signal.timestamp)}
                    </Text>
                  </View>
                </View>

                <View style={styles.signalBody}>
                  <View style={styles.priceInfo}>
                    <Text style={[styles.priceLabel, { color: theme.textSecondary }]}>
                      Entry Price
                    </Text>
                    <Text style={[styles.priceValue, { color: theme.text }]}>
                      ${signal.price.toFixed(2)}
                    </Text>
                  </View>

                  <View style={styles.targetsContainer}>
                    <View style={styles.targetItem}>
                      <Text style={[styles.targetLabel, { color: theme.textSecondary }]}>
                        Stop Loss
                      </Text>
                      <Text style={[styles.targetValue, { color: '#ef4444' }]}>
                        ${signal.stopLoss.toFixed(2)}
                      </Text>
                    </View>

                    <View style={styles.targetItem}>
                      <Text style={[styles.targetLabel, { color: theme.textSecondary }]}>
                        Target 1
                      </Text>
                      <Text style={[styles.targetValue, { color: '#10b981' }]}>
                        ${signal.takeProfit[0].toFixed(2)}
                      </Text>
                    </View>
                  </View>
                </View>

                <View style={styles.signalFooter}>
                  <View style={styles.confidenceContainer}>
                    <Text style={[styles.confidenceLabel, { color: theme.textSecondary }]}>
                      AI Confidence
                    </Text>
                    <View style={styles.confidenceBar}>
                      <View
                        style={[
                          styles.confidenceFill,
                          {
                            width: `${signal.confidence * 100}%`,
                            backgroundColor: getConfidenceColor(signal.confidence),
                          },
                        ]}
                      />
                    </View>
                    <Text
                      style={[
                        styles.confidenceText,
                        { color: getConfidenceColor(signal.confidence) },
                      ]}
                    >
                      {(signal.confidence * 100).toFixed(0)}%
                    </Text>
                  </View>

                  <View style={styles.reasoningContainer}>
                    <Text style={[styles.reasoningLabel, { color: theme.textSecondary }]}>
                      AI Reasoning:
                    </Text>
                    <Text style={[styles.reasoningText, { color: theme.text }]}>
                      {signal.aiReasoning}
                    </Text>
                  </View>

                  <View style={styles.signalActions}>
                    <TouchableOpacity
                      style={[styles.actionButton, { backgroundColor: theme.primary }]}
                      onPress={() => handleSubscribeToSignal(signal.id)}
                    >
                      <Text style={styles.actionButtonText}>Trade Now</Text>
                    </TouchableOpacity>

                    <TouchableOpacity
                      style={[styles.actionButton, { backgroundColor: 'transparent', borderWidth: 1, borderColor: theme.border }]}
                      onPress={() => handleSubscribeToSignal(signal.id)}
                    >
                      <Text style={[styles.actionButtonText, { color: theme.primary }]}>
                        Subscribe
                      </Text>
                    </TouchableOpacity>
                  </View>
                </View>
              </TouchableOpacity>
            ))}
        </View>

        {/* AI Insights */}
        <View style={[styles.insightsCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>
            🧠 AI Market Insights
          </Text>

          <View style={styles.insight}>
            <Text style={[styles.insightTitle, { color: theme.text }]}>
              Market Regime Detection
            </Text>
            <Text style={[styles.insightText, { color: theme.textSecondary }]}>
              Current market is in <Text style={{ color: '#10b981' }}>BULLISH</Text> regime with high momentum signals
            </Text>
          </View>

          <View style={styles.insight}>
            <Text style={[styles.insightTitle, { color: theme.text }]}>
              Volatility Forecast
            </Text>
            <Text style={[styles.insightText, { color: theme.textSecondary }]}>
              Expected volatility to <Text style={{ color: '#f59e0b' }}>increase 15%</Text> in next 2 hours due to FOMC announcement
            </Text>
          </View>

          <View style={styles.insight}>
            <Text style={[styles.insightTitle, { color: theme.text }]}>
              Sector Rotation
            </Text>
            <Text style={[styles.insightText, { color: theme.textSecondary }]}>
              AI models detect rotation from <Text style={{ color: '#ef4444' }}>Tech</Text> to <Text style={{ color: '#10b981' }}>Healthcare</Text>
            </Text>
          </View>
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
  performanceCard: {
    margin: 20,
    marginTop: 10,
    padding: 25,
    borderRadius: 20,
    elevation: 5,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  performanceTitle: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 20,
    textAlign: 'center',
  },
  performanceStats: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  performanceStat: {
    alignItems: 'center',
  },
  performanceValue: {
    color: '#fff',
    fontSize: 24,
    fontWeight: 'bold',
    marginBottom: 4,
  },
  performanceLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 12,
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
    marginBottom: 12,
  },
  priceLabel: {
    fontSize: 14,
  },
  priceValue: {
    fontSize: 16,
    fontWeight: '600',
  },
  targetsContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  targetItem: {
    alignItems: 'center',
  },
  targetLabel: {
    fontSize: 12,
    marginBottom: 4,
  },
  targetValue: {
    fontSize: 14,
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
  insightsCard: {
    margin: 20,
    marginTop: 0,
    padding: 20,
    borderRadius: 20,
    elevation: 2,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.22,
    shadowRadius: 2.22,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 16,
  },
  insight: {
    marginBottom: 16,
  },
  insightTitle: {
    fontSize: 14,
    fontWeight: '600',
    marginBottom: 4,
  },
  insightText: {
    fontSize: 13,
    lineHeight: 18,
  },
});