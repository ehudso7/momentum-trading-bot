/**
 * TradingScreen — "Trade on your broker" hand-off (App Store 3.2.1 compliant).
 *
 * This screen intentionally does NOT place orders. Momentum is positioned as
 * an AI market-intelligence and signals product; execution happens on the
 * user's own regulated broker. Rationale (see AUDIT sheet Momentum_Trading):
 *
 *   Apple App Review Guideline 3.2.1: apps used for financial trading,
 *   investing, or money management must be submitted by the regulated
 *   financial institution performing those services. A generic developer
 *   entity that ships an in-app order-placement flow gets rejected.
 *
 * The pattern here mirrors TipRanks and TradingView on iOS: the user reads
 * the signal + thesis + confidence band in-app, then taps a broker deep-link
 * (Robinhood, Webull, Interactive Brokers, Fidelity) to execute in their own
 * regulated account. No fund custody, no order routing, no advice claim.
 *
 * Investment Advisers Act 1940 posture: content is impersonal, published on
 * a regular schedule, and does not constitute personalized advice — the
 * "publisher's exclusion" rationale used by Motley Fool / Seeking Alpha.
 * Ship with disclaimers visible and never suggest an action for a specific
 * user's specific account.
 */
import React, { useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  Linking,
  Alert,
  Dimensions,
} from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { useQuery } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { LineChart } from 'react-native-chart-kit';
import { useTheme } from '../contexts/ThemeContext';
import { useWebSocket } from '../contexts/WebSocketContext';
import { api } from '../services/api';

const { width } = Dimensions.get('window');

// --------------------------------------------------------------------------
// Broker deep-link registry.
//
// Each broker exposes a universal link that opens the broker's app to the
// symbol's trade ticket. If the app is not installed, iOS falls through to
// the broker's website via the same URL. Users complete the transaction in
// their own regulated brokerage account; Momentum never touches funds or
// orders. Adding a broker here surfaces it in the "Trade on your broker"
// panel below — no other UI work is required.
// --------------------------------------------------------------------------
type Broker = {
  key: string;
  name: string;
  color: string;
  buildUrl: (symbol: string) => string;
};

const BROKERS: Broker[] = [
  {
    key: 'robinhood',
    name: 'Robinhood',
    color: '#00C805',
    buildUrl: (symbol) => `https://robinhood.com/stocks/${symbol}`,
  },
  {
    key: 'webull',
    name: 'Webull',
    color: '#0074E4',
    buildUrl: (symbol) => `https://www.webull.com/quote/${symbol}`,
  },
  {
    key: 'ibkr',
    name: 'Interactive Brokers',
    color: '#B00E1E',
    buildUrl: (symbol) => `https://www.interactivebrokers.com/en/trading/orders.php?symbol=${symbol}`,
  },
  {
    key: 'fidelity',
    name: 'Fidelity',
    color: '#568203',
    buildUrl: (symbol) => `https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=${symbol}`,
  },
];

export default function TradingScreen() {
  const { theme } = useTheme();
  const { subscribe } = useWebSocket();

  const [selectedSymbol, setSelectedSymbol] = useState('AAPL');

  React.useEffect(() => {
    subscribe(`market:${selectedSymbol}`);
  }, [selectedSymbol, subscribe]);

  useQuery({
    queryKey: ['market', selectedSymbol],
    queryFn: () => api.getMarketData(selectedSymbol),
  });

  const watchlist = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'GOOGL', 'AMZN'];

  const chartData = {
    labels: ['9:30', '10:00', '10:30', '11:00', '11:30', '12:00'],
    datasets: [{ data: [150, 152, 148, 155, 153, 157] }],
  };

  // Handle broker deep-link. Try to open the broker's app or website; if the
  // system rejects the URL for any reason we surface a friendly explanation
  // rather than crashing.
  const openBroker = async (broker: Broker) => {
    const url = broker.buildUrl(selectedSymbol);
    try {
      const supported = await Linking.canOpenURL(url);
      if (!supported) {
        Alert.alert(
          `${broker.name} isn't available`,
          `We couldn't open ${broker.name} on this device. Try opening ${broker.name} directly in your browser.`,
        );
        return;
      }
      await Linking.openURL(url);
    } catch (err) {
      Alert.alert(
        'Could not open broker',
        err instanceof Error ? err.message : 'Please try again.',
      );
    }
  };

  return (
    <SafeAreaView style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView showsVerticalScrollIndicator={false}>
        {/* Header */}
        <View style={styles.header}>
          <Text style={[styles.title, { color: theme.text }]}>Trade</Text>
        </View>

        {/* Non-advice disclaimer.
            Rendered at the top of every visit to the Trade tab so it is
            impossible to reach the broker hand-offs without seeing it.
            Wording follows the Motley Fool / Seeking Alpha "publisher's
            exclusion" pattern under the Investment Advisers Act 1940. */}
        <View style={[styles.disclaimer, { backgroundColor: theme.card, borderColor: theme.border }]}>
          <Text style={[styles.disclaimerLabel, { color: theme.textSecondary }]}>
            Educational information, not personalized advice
          </Text>
          <Text style={[styles.disclaimerBody, { color: theme.text }]}>
            Momentum publishes impersonal market intelligence and signals for
            educational purposes only. It is not a broker-dealer, is not
            registered as an investment adviser, and does not place orders on
            your behalf. Use your own regulated brokerage account and consult a
            licensed professional before acting.
          </Text>
        </View>

        {/* Watchlist */}
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          style={styles.watchlist}
          contentContainerStyle={styles.watchlistContent}
        >
          {watchlist.map((symbol) => (
            <TouchableOpacity
              key={symbol}
              onPress={() => setSelectedSymbol(symbol)}
              style={[
                styles.watchlistItem,
                {
                  backgroundColor:
                    selectedSymbol === symbol ? theme.primary : theme.card,
                },
              ]}
            >
              <Text
                style={[
                  styles.watchlistSymbol,
                  { color: selectedSymbol === symbol ? '#fff' : theme.text },
                ]}
              >
                {symbol}
              </Text>
              <Text
                style={[
                  styles.watchlistPrice,
                  {
                    color:
                      selectedSymbol === symbol
                        ? 'rgba(255,255,255,0.8)'
                        : theme.textSecondary,
                  },
                ]}
              >
                $150.25
              </Text>
              <Text
                style={[
                  styles.watchlistChange,
                  { color: selectedSymbol === symbol ? '#fff' : '#10b981' },
                ]}
              >
                +2.5%
              </Text>
            </TouchableOpacity>
          ))}
        </ScrollView>

        {/* Current stock info */}
        <LinearGradient colors={['#667eea', '#764ba2']} style={styles.stockCard}>
          <View style={styles.stockHeader}>
            <View>
              <Text style={styles.stockSymbol}>{selectedSymbol}</Text>
              <Text style={styles.stockCompany}>Apple Inc.</Text>
            </View>
            <View style={styles.stockPriceContainer}>
              <Text style={styles.stockPrice}>$150.25</Text>
              <Text style={styles.stockChange}>+$2.50 (+1.69%)</Text>
            </View>
          </View>

          <View style={styles.stockStats}>
            <View style={styles.stockStat}>
              <Text style={styles.stockStatLabel}>Open</Text>
              <Text style={styles.stockStatValue}>$148.25</Text>
            </View>
            <View style={styles.stockStat}>
              <Text style={styles.stockStatLabel}>High</Text>
              <Text style={styles.stockStatValue}>$152.10</Text>
            </View>
            <View style={styles.stockStat}>
              <Text style={styles.stockStatLabel}>Low</Text>
              <Text style={styles.stockStatValue}>$147.80</Text>
            </View>
            <View style={styles.stockStat}>
              <Text style={styles.stockStatLabel}>Volume</Text>
              <Text style={styles.stockStatValue}>2.5M</Text>
            </View>
          </View>
        </LinearGradient>

        {/* Chart */}
        <View style={[styles.chartCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>
            Price Chart — {selectedSymbol}
          </Text>

          <LineChart
            data={chartData}
            width={width - 40}
            height={200}
            chartConfig={{
              backgroundColor: theme.card,
              backgroundGradientFrom: theme.card,
              backgroundGradientTo: theme.card,
              decimalPlaces: 2,
              color: (opacity = 1) => `rgba(102, 126, 234, ${opacity})`,
              labelColor: () => theme.textSecondary,
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

        {/* Trade on your broker — hand-off panel replaces the previous
            in-app order form. Each card deep-links to the corresponding
            broker's ticket for the selected symbol. */}
        <View style={[styles.brokerCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>
            Trade on your broker
          </Text>
          <Text style={[styles.brokerSubtitle, { color: theme.textSecondary }]}>
            Open {selectedSymbol} in the brokerage account you already use.
            Momentum does not route or execute orders.
          </Text>

          <View style={styles.brokerList}>
            {BROKERS.map((broker) => (
              <TouchableOpacity
                key={broker.key}
                onPress={() => openBroker(broker)}
                style={[
                  styles.brokerItem,
                  { backgroundColor: theme.background, borderColor: theme.border },
                ]}
                accessibilityRole="button"
                accessibilityLabel={`Open ${broker.name} to trade ${selectedSymbol}`}
              >
                <View style={[styles.brokerBadge, { backgroundColor: broker.color }]}>
                  <Text style={styles.brokerBadgeText}>
                    {broker.name.charAt(0)}
                  </Text>
                </View>
                <View style={styles.brokerText}>
                  <Text style={[styles.brokerName, { color: theme.text }]}>
                    {broker.name}
                  </Text>
                  <Text style={[styles.brokerAction, { color: theme.textSecondary }]}>
                    Open {selectedSymbol} ticket
                  </Text>
                </View>
                <Text style={[styles.brokerChevron, { color: theme.textSecondary }]}>
                  ›
                </Text>
              </TouchableOpacity>
            ))}
          </View>
        </View>

        {/* Footer disclaimer — repeated at the bottom so it is visible
            regardless of how far a user has scrolled. */}
        <View style={styles.footerNote}>
          <Text style={[styles.footerText, { color: theme.textSecondary }]}>
            Signals, prices, and news are provided for informational and
            educational purposes only. Past performance does not guarantee
            future results. Investing involves risk of loss.
          </Text>
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
  disclaimer: {
    marginHorizontal: 20,
    padding: 16,
    borderRadius: 12,
    borderWidth: 1,
    marginBottom: 8,
  },
  disclaimerLabel: {
    fontSize: 11,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.6,
    marginBottom: 6,
  },
  disclaimerBody: {
    fontSize: 12,
    lineHeight: 18,
  },
  watchlist: {
    marginTop: 12,
  },
  watchlistContent: {
    paddingHorizontal: 20,
    gap: 10,
  },
  watchlistItem: {
    padding: 12,
    borderRadius: 12,
    minWidth: 90,
    alignItems: 'center',
  },
  watchlistSymbol: {
    fontSize: 14,
    fontWeight: '700',
  },
  watchlistPrice: {
    fontSize: 12,
    marginTop: 4,
  },
  watchlistChange: {
    fontSize: 12,
    marginTop: 2,
    fontWeight: '600',
  },
  stockCard: {
    margin: 20,
    padding: 20,
    borderRadius: 16,
  },
  stockHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
  },
  stockSymbol: {
    fontSize: 24,
    fontWeight: '700',
    color: '#fff',
  },
  stockCompany: {
    fontSize: 12,
    color: 'rgba(255,255,255,0.8)',
    marginTop: 2,
  },
  stockPriceContainer: {
    alignItems: 'flex-end',
  },
  stockPrice: {
    fontSize: 24,
    fontWeight: '700',
    color: '#fff',
  },
  stockChange: {
    fontSize: 12,
    color: 'rgba(255,255,255,0.9)',
    marginTop: 2,
  },
  stockStats: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginTop: 16,
  },
  stockStat: {
    alignItems: 'center',
  },
  stockStatLabel: {
    fontSize: 11,
    color: 'rgba(255,255,255,0.7)',
  },
  stockStatValue: {
    fontSize: 13,
    color: '#fff',
    fontWeight: '600',
    marginTop: 2,
  },
  chartCard: {
    marginHorizontal: 20,
    padding: 16,
    borderRadius: 16,
    marginBottom: 16,
  },
  sectionTitle: {
    fontSize: 16,
    fontWeight: '700',
    marginBottom: 12,
  },
  chart: {
    borderRadius: 12,
  },
  brokerCard: {
    marginHorizontal: 20,
    padding: 16,
    borderRadius: 16,
    marginBottom: 16,
  },
  brokerSubtitle: {
    fontSize: 12,
    marginBottom: 12,
    lineHeight: 18,
  },
  brokerList: {
    gap: 10,
  },
  brokerItem: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 12,
    borderRadius: 12,
    borderWidth: 1,
    gap: 12,
  },
  brokerBadge: {
    width: 36,
    height: 36,
    borderRadius: 8,
    justifyContent: 'center',
    alignItems: 'center',
  },
  brokerBadgeText: {
    color: '#fff',
    fontWeight: '700',
    fontSize: 16,
  },
  brokerText: {
    flex: 1,
  },
  brokerName: {
    fontSize: 14,
    fontWeight: '600',
  },
  brokerAction: {
    fontSize: 12,
    marginTop: 2,
  },
  brokerChevron: {
    fontSize: 24,
    marginRight: 6,
  },
  footerNote: {
    paddingHorizontal: 20,
    paddingVertical: 8,
  },
  footerText: {
    fontSize: 11,
    lineHeight: 16,
  },
});
