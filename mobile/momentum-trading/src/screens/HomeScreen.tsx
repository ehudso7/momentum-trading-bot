import React from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  RefreshControl,
  Dimensions,
} from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { useQuery } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { LineChart } from 'react-native-chart-kit';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { api } from '../services/api';

const { width } = Dimensions.get('window');

export default function HomeScreen() {
  const { theme } = useTheme();
  const { user } = useAuth();
  const [refreshing, setRefreshing] = React.useState(false);

  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: api.getPortfolio,
  });

  const { data: signals } = useQuery({
    queryKey: ['signals', 'latest'],
    queryFn: api.getLatestSignals,
  });

  const onRefresh = React.useCallback(async () => {
    setRefreshing(true);
    // Refetch data
    setTimeout(() => setRefreshing(false), 2000);
  }, []);

  const chartData = {
    labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Today'],
    datasets: [{
      data: [98000, 99200, 98500, 101000, 102500, 105000],
    }],
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
          <Text style={[styles.greeting, { color: theme.textSecondary }]}>
            Welcome back,
          </Text>
          <Text style={[styles.userName, { color: theme.text }]}>
            {user?.name || 'Trader'}
          </Text>
        </View>

        {/* Portfolio Card */}
        <LinearGradient
          colors={['#667eea', '#764ba2']}
          start={{ x: 0, y: 0 }}
          end={{ x: 1, y: 1 }}
          style={styles.portfolioCard}
        >
          <Text style={styles.portfolioLabel}>Total Portfolio Value</Text>
          <Text style={styles.portfolioValue}>
            ${portfolio?.totalValue?.toLocaleString() || '105,000'}
          </Text>
          <View style={styles.portfolioStats}>
            <View style={styles.stat}>
              <Text style={styles.statLabel}>Today's P&L</Text>
              <Text style={[styles.statValue, styles.profit]}>+$2,500</Text>
            </View>
            <View style={styles.stat}>
              <Text style={styles.statLabel}>Return</Text>
              <Text style={[styles.statValue, styles.profit]}>+2.44%</Text>
            </View>
          </View>
        </LinearGradient>

        {/* Performance Chart */}
        <View style={[styles.chartCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>
            Performance
          </Text>
          <LineChart
            data={chartData}
            width={width - 40}
            height={200}
            chartConfig={{
              backgroundColor: theme.card,
              backgroundGradientFrom: theme.card,
              backgroundGradientTo: theme.card,
              decimalPlaces: 0,
              color: (opacity = 1) => `rgba(102, 126, 234, ${opacity})`,
              labelColor: (opacity = 1) => theme.textSecondary,
              style: { borderRadius: 16 },
              propsForDots: {
                r: '4',
                strokeWidth: '2',
                stroke: '#667eea',
              },
            }}
            bezier
            style={styles.chart}
          />
        </View>

        {/* Active Signals */}
        <View style={[styles.signalsCard, { backgroundColor: theme.card }]}>
          <View style={styles.sectionHeader}>
            <Text style={[styles.sectionTitle, { color: theme.text }]}>
              Active Signals
            </Text>
            <TouchableOpacity>
              <Text style={styles.seeAll}>See All</Text>
            </TouchableOpacity>
          </View>

          {signals?.map((signal: any, index: number) => (
            <TouchableOpacity
              key={index}
              style={[styles.signalItem, { borderColor: theme.border }]}
            >
              <View style={styles.signalLeft}>
                <Text style={[styles.signalSymbol, { color: theme.text }]}>
                  {signal.symbol}
                </Text>
                <Text style={[styles.signalType, { color: theme.textSecondary }]}>
                  {signal.type}
                </Text>
              </View>
              <View style={styles.signalRight}>
                <Text style={[styles.signalPrice, { color: theme.text }]}>
                  ${signal.price}
                </Text>
                <Text
                  style={[
                    styles.signalConfidence,
                    { color: signal.confidence > 0.8 ? '#10b981' : '#f59e0b' },
                  ]}
                >
                  {(signal.confidence * 100).toFixed(0)}% confidence
                </Text>
              </View>
            </TouchableOpacity>
          )) || (
            <Text style={[styles.noSignals, { color: theme.textSecondary }]}>
              No active signals
            </Text>
          )}
        </View>

        {/* Quick Actions */}
        <View style={styles.quickActions}>
          <TouchableOpacity
            style={[styles.actionButton, { backgroundColor: theme.primary }]}
          >
            <Text style={styles.actionButtonText}>Trade Now</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.actionButton, { backgroundColor: theme.card }]}
          >
            <Text style={[styles.actionButtonText, { color: theme.primary }]}>
              View Portfolio
            </Text>
          </TouchableOpacity>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  header: {
    padding: 20,
    paddingBottom: 10,
  },
  greeting: {
    fontSize: 14,
    marginBottom: 4,
  },
  userName: {
    fontSize: 24,
    fontWeight: 'bold',
  },
  portfolioCard: {
    margin: 20,
    marginTop: 10,
    padding: 20,
    borderRadius: 20,
    elevation: 5,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  portfolioLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 14,
    marginBottom: 8,
  },
  portfolioValue: {
    color: '#fff',
    fontSize: 32,
    fontWeight: 'bold',
    marginBottom: 20,
  },
  portfolioStats: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  stat: {
    flex: 1,
  },
  statLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 12,
    marginBottom: 4,
  },
  statValue: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
  },
  profit: {
    color: '#10b981',
  },
  chartCard: {
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
    marginBottom: 15,
  },
  chart: {
    marginLeft: -10,
    borderRadius: 16,
  },
  signalsCard: {
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
  sectionHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 15,
  },
  seeAll: {
    color: '#667eea',
    fontSize: 14,
    fontWeight: '500',
  },
  signalItem: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 12,
    borderBottomWidth: 1,
  },
  signalLeft: {
    flex: 1,
  },
  signalSymbol: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 2,
  },
  signalType: {
    fontSize: 12,
  },
  signalRight: {
    alignItems: 'flex-end',
  },
  signalPrice: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 2,
  },
  signalConfidence: {
    fontSize: 12,
    fontWeight: '500',
  },
  noSignals: {
    textAlign: 'center',
    paddingVertical: 20,
  },
  quickActions: {
    flexDirection: 'row',
    padding: 20,
    paddingTop: 0,
    gap: 10,
  },
  actionButton: {
    flex: 1,
    paddingVertical: 15,
    borderRadius: 12,
    alignItems: 'center',
  },
  actionButtonText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
});