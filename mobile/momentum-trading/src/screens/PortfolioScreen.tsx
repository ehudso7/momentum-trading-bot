import React, { useState } from 'react';
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
import { PieChart, LineChart } from 'react-native-chart-kit';
import { useTheme } from '../contexts/ThemeContext';
import { api } from '../services/api';
import {
  fmtMoney,
  fmtSignedMoney,
  fmtPct,
  fmtQuantity,
  fmtText,
  changeColor,
} from '../utils/format';
import DataUnavailable from '../components/DataUnavailable';

const { width } = Dimensions.get('window');

export default function PortfolioScreen() {
  const { theme } = useTheme();
  const [refreshing, setRefreshing] = useState(false);
  const [selectedPeriod, setSelectedPeriod] = useState('1D');

  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: api.getPortfolio,
  });

  const { data: positions } = useQuery({
    queryKey: ['positions'],
    queryFn: api.getPositions,
  });

  const { data: performance } = useQuery({
    queryKey: ['performance', selectedPeriod],
    queryFn: () => api.getPerformance(selectedPeriod.toLowerCase()),
  });

  const onRefresh = React.useCallback(async () => {
    setRefreshing(true);
    setTimeout(() => setRefreshing(false), 2000);
  }, []);

  const periods = ['1D', '1W', '1M', '3M', '1Y', 'ALL'];

  // Holdings come from the API only. There is deliberately no sample
  // fallback here: see components/DataUnavailable.tsx.
  const holdings = positions ?? [];

  // The performance series the chart needs is not yet returned in a charted
  // shape by the backend, so the chart renders an unavailable state rather
  // than a fabricated curve.
  const performanceSeries: { labels: string[]; datasets: { data: number[] }[] } | null = null;

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
          <Text style={[styles.title, { color: theme.text }]}>Portfolio</Text>
          <TouchableOpacity style={styles.settingsButton}>
            <Text style={[styles.settingsIcon, { color: theme.textSecondary }]}>⚙️</Text>
          </TouchableOpacity>
        </View>

        {/* Portfolio Summary */}
        <LinearGradient
          colors={['#667eea', '#764ba2']}
          style={styles.summaryCard}
        >
          <Text style={styles.summaryLabel}>Total Portfolio Value</Text>
          <Text style={styles.summaryValue}>{fmtMoney(portfolio?.totalValue)}</Text>

          <View style={styles.summaryStats}>
            <View style={styles.stat}>
              <Text style={styles.statValue}>{fmtSignedMoney(portfolio?.dayChange)}</Text>
              <Text style={styles.statLabel}>Today's P&L</Text>
            </View>
            <View style={styles.stat}>
              <Text style={styles.statValue}>{fmtPct(portfolio?.dayChangePercent)}</Text>
              <Text style={styles.statLabel}>Today's Change</Text>
            </View>
          </View>
        </LinearGradient>

        {/* Performance Chart */}
        <View style={[styles.chartCard, { backgroundColor: theme.card }]}>
          <View style={styles.chartHeader}>
            <Text style={[styles.sectionTitle, { color: theme.text }]}>Performance</Text>
            <View style={styles.periodSelector}>
              {periods.map((period) => (
                <TouchableOpacity
                  key={period}
                  onPress={() => setSelectedPeriod(period)}
                  style={[
                    styles.periodButton,
                    selectedPeriod === period && styles.periodButtonActive,
                  ]}
                >
                  <Text
                    style={[
                      styles.periodText,
                      { color: selectedPeriod === period ? '#fff' : theme.textSecondary },
                    ]}
                  >
                    {period}
                  </Text>
                </TouchableOpacity>
              ))}
            </View>
          </View>

          {performanceSeries ? (
            <LineChart
              data={performanceSeries}
              width={width - 40}
              height={200}
              chartConfig={{
                backgroundColor: theme.card,
                backgroundGradientFrom: theme.card,
                backgroundGradientTo: theme.card,
                decimalPlaces: 0,
                color: (opacity = 1) => `rgba(102, 126, 234, ${opacity})`,
                labelColor: () => theme.textSecondary,
                style: { borderRadius: 16 },
                propsForDots: { r: '4', strokeWidth: '2', stroke: '#667eea' },
              }}
              bezier
              style={styles.chart}
            />
          ) : (
            <DataUnavailable
              title="Performance history unavailable"
              detail="Connect an account to chart real performance."
            />
          )}
        </View>

        {/* Asset Allocation */}
        <View style={[styles.allocationCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>Asset Allocation</Text>

          <DataUnavailable
            title="Allocation unavailable"
            detail="Allocation is derived from live holdings."
          />
        </View>

        {/* Holdings */}
        <View style={[styles.holdingsCard, { backgroundColor: theme.card }]}>
          <View style={styles.holdingsHeader}>
            <Text style={[styles.sectionTitle, { color: theme.text }]}>Holdings</Text>
            <TouchableOpacity>
              <Text style={[styles.viewAll, { color: theme.primary }]}>View All</Text>
            </TouchableOpacity>
          </View>

          {holdings.length === 0 ? (
            <DataUnavailable title="No holdings to display" />
          ) : holdings.map((position) => (
            <TouchableOpacity
              key={position.symbol}
              style={[styles.positionItem, { borderBottomColor: theme.border }]}
            >
              <View style={styles.positionLeft}>
                <Text style={[styles.positionSymbol, { color: theme.text }]}>
                  {position.symbol}
                </Text>
                <Text style={[styles.positionName, { color: theme.textSecondary }]}>
                  {`${fmtQuantity(position.quantity)} shares`}
                </Text>
              </View>

              <View style={styles.positionCenter}>
                <Text style={[styles.positionValue, { color: theme.text }]}>
                  {fmtMoney(position.currentPrice)}
                </Text>
                <Text style={[styles.positionCompany, { color: theme.textSecondary }]}>
                  {fmtText(position.side)}
                </Text>
              </View>

              <View style={styles.positionRight}>
                <Text
                  style={[
                    styles.positionChange,
                    { color: changeColor(position.unrealizedPnL, theme.textSecondary) },
                  ]}
                >
                  {fmtSignedMoney(position.unrealizedPnL)}
                </Text>
                <Text
                  style={[
                    styles.positionPercent,
                    { color: theme.textSecondary },
                  ]}
                >
                  {position.entryPrice !== undefined
                    ? `entry ${fmtMoney(position.entryPrice)}`
                    : ''}
                </Text>
              </View>
            </TouchableOpacity>
          ))}
        </View>

        {/* Quick Actions */}
        <View style={styles.actions}>
          <TouchableOpacity
            style={[styles.actionButton, { backgroundColor: theme.primary }]}
          >
            <Text style={styles.actionText}>Add Funds</Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.actionButton, { backgroundColor: theme.card, borderWidth: 2, borderColor: theme.primary }]}
          >
            <Text style={[styles.actionText, { color: theme.primary }]}>Rebalance</Text>
          </TouchableOpacity>
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
  settingsButton: {
    padding: 8,
  },
  settingsIcon: {
    fontSize: 20,
  },
  summaryCard: {
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
  summaryLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 14,
    marginBottom: 8,
  },
  summaryValue: {
    color: '#fff',
    fontSize: 36,
    fontWeight: 'bold',
    marginBottom: 20,
  },
  summaryStats: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  stat: {
    flex: 1,
  },
  statValue: {
    color: '#fff',
    fontSize: 20,
    fontWeight: '600',
    marginBottom: 4,
  },
  statLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 12,
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
  chartHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  periodSelector: {
    flexDirection: 'row',
  },
  periodButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    marginLeft: 4,
    borderRadius: 8,
  },
  periodButtonActive: {
    backgroundColor: '#667eea',
  },
  periodText: {
    fontSize: 12,
    fontWeight: '500',
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
  },
  chart: {
    marginLeft: -10,
    borderRadius: 16,
  },
  allocationCard: {
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
  holdingsCard: {
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
  holdingsHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  viewAll: {
    fontSize: 14,
    fontWeight: '500',
  },
  positionItem: {
    flexDirection: 'row',
    paddingVertical: 16,
    borderBottomWidth: 1,
  },
  positionLeft: {
    flex: 1,
  },
  positionSymbol: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 4,
  },
  positionName: {
    fontSize: 12,
  },
  positionCenter: {
    flex: 1,
    alignItems: 'center',
  },
  positionValue: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 4,
  },
  positionCompany: {
    fontSize: 12,
    textAlign: 'center',
  },
  positionRight: {
    flex: 1,
    alignItems: 'flex-end',
  },
  positionChange: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 4,
  },
  positionPercent: {
    fontSize: 12,
    fontWeight: '500',
  },
  actions: {
    flexDirection: 'row',
    padding: 20,
    paddingTop: 0,
    gap: 12,
  },
  actionButton: {
    flex: 1,
    paddingVertical: 16,
    borderRadius: 12,
    alignItems: 'center',
  },
  actionText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
});