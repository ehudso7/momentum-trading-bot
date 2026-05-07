import React, { useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  TextInput,
  Alert,
  Dimensions,
} from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { LineChart } from 'react-native-chart-kit';
import { useTheme } from '../contexts/ThemeContext';
import { useWebSocket } from '../contexts/WebSocketContext';
import { api } from '../services/api';

const { width } = Dimensions.get('window');

export default function TradingScreen() {
  const { theme } = useTheme();
  const { subscribe, lastMessage } = useWebSocket();
  const queryClient = useQueryClient();

  const [selectedSymbol, setSelectedSymbol] = useState('AAPL');
  const [orderType, setOrderType] = useState('market');
  const [side, setSide] = useState('buy');
  const [quantity, setQuantity] = useState('10');
  const [price, setPrice] = useState('');

  React.useEffect(() => {
    subscribe(`market:${selectedSymbol}`);
  }, [selectedSymbol, subscribe]);

  const { data: marketData } = useQuery({
    queryKey: ['market', selectedSymbol],
    queryFn: () => api.getMarketData(selectedSymbol),
  });

  const { data: orders } = useQuery({
    queryKey: ['orders'],
    queryFn: api.getOrders,
  });

  const placeOrderMutation = useMutation({
    mutationFn: api.placeOrder,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['portfolio'] });
      Alert.alert('Success', 'Order placed successfully');
      setQuantity('10');
      setPrice('');
    },
    onError: (error: any) => {
      Alert.alert('Error', error.message || 'Failed to place order');
    },
  });

  const handlePlaceOrder = () => {
    if (!quantity || (orderType === 'limit' && !price)) {
      Alert.alert('Error', 'Please fill in all required fields');
      return;
    }

    const order = {
      symbol: selectedSymbol,
      side,
      type: orderType,
      quantity: parseFloat(quantity),
      price: orderType === 'limit' ? parseFloat(price) : undefined,
    };

    placeOrderMutation.mutate(order);
  };

  const watchlist = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'GOOGL', 'AMZN'];

  const chartData = {
    labels: ['9:30', '10:00', '10:30', '11:00', '11:30', '12:00'],
    datasets: [{
      data: [150, 152, 148, 155, 153, 157],
    }],
  };

  const mockOrders = [
    { id: '1', symbol: 'AAPL', side: 'buy', quantity: 10, price: 150.25, status: 'filled' },
    { id: '2', symbol: 'TSLA', side: 'sell', quantity: 5, price: 250.00, status: 'pending' },
    { id: '3', symbol: 'NVDA', side: 'buy', quantity: 15, price: 520.75, status: 'filled' },
  ];

  return (
    <SafeAreaView style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView showsVerticalScrollIndicator={false}>
        {/* Header */}
        <View style={styles.header}>
          <Text style={[styles.title, { color: theme.text }]}>Trading</Text>
          <TouchableOpacity style={styles.searchButton}>
            <Text style={[styles.searchIcon, { color: theme.textSecondary }]}>🔍</Text>
          </TouchableOpacity>
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
                  backgroundColor: selectedSymbol === symbol ? theme.primary : theme.card,
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
                  { color: selectedSymbol === symbol ? 'rgba(255,255,255,0.8)' : theme.textSecondary },
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

        {/* Current Stock Info */}
        <LinearGradient
          colors={['#667eea', '#764ba2']}
          style={styles.stockCard}
        >
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
            Price Chart - {selectedSymbol}
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

        {/* Order Form */}
        <View style={[styles.orderCard, { backgroundColor: theme.card }]}>
          <Text style={[styles.sectionTitle, { color: theme.text }]}>Place Order</Text>

          {/* Order Type Selector */}
          <View style={styles.orderTypeContainer}>
            {['buy', 'sell'].map((type) => (
              <TouchableOpacity
                key={type}
                onPress={() => setSide(type)}
                style={[
                  styles.orderTypeButton,
                  {
                    backgroundColor: side === type ?
                      (type === 'buy' ? '#10b981' : '#ef4444') : theme.background,
                  },
                ]}
              >
                <Text
                  style={[
                    styles.orderTypeText,
                    { color: side === type ? '#fff' : theme.text },
                  ]}
                >
                  {type.toUpperCase()}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          {/* Market/Limit Selector */}
          <View style={styles.orderTypeContainer}>
            {['market', 'limit'].map((type) => (
              <TouchableOpacity
                key={type}
                onPress={() => setOrderType(type)}
                style={[
                  styles.orderTypeButton,
                  {
                    backgroundColor: orderType === type ? theme.primary : theme.background,
                  },
                ]}
              >
                <Text
                  style={[
                    styles.orderTypeText,
                    { color: orderType === type ? '#fff' : theme.text },
                  ]}
                >
                  {type.toUpperCase()}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          {/* Quantity Input */}
          <View style={styles.inputContainer}>
            <Text style={[styles.inputLabel, { color: theme.text }]}>Quantity</Text>
            <TextInput
              style={[styles.input, { backgroundColor: theme.background, color: theme.text }]}
              value={quantity}
              onChangeText={setQuantity}
              placeholder="Number of shares"
              placeholderTextColor={theme.textSecondary}
              keyboardType="numeric"
            />
          </View>

          {/* Price Input (for limit orders) */}
          {orderType === 'limit' && (
            <View style={styles.inputContainer}>
              <Text style={[styles.inputLabel, { color: theme.text }]}>Price</Text>
              <TextInput
                style={[styles.input, { backgroundColor: theme.background, color: theme.text }]}
                value={price}
                onChangeText={setPrice}
                placeholder="Limit price"
                placeholderTextColor={theme.textSecondary}
                keyboardType="numeric"
              />
            </View>
          )}

          {/* Order Summary */}
          <View style={[styles.orderSummary, { backgroundColor: theme.background }]}>
            <View style={styles.summaryRow}>
              <Text style={[styles.summaryLabel, { color: theme.textSecondary }]}>
                Estimated Total
              </Text>
              <Text style={[styles.summaryValue, { color: theme.text }]}>
                ${((parseFloat(quantity) || 0) * 150.25).toFixed(2)}
              </Text>
            </View>
          </View>

          {/* Place Order Button */}
          <TouchableOpacity
            onPress={handlePlaceOrder}
            disabled={placeOrderMutation.isPending}
            style={[
              styles.placeOrderButton,
              { opacity: placeOrderMutation.isPending ? 0.6 : 1 }
            ]}
          >
            <LinearGradient
              colors={side === 'buy' ? ['#10b981', '#059669'] : ['#ef4444', '#dc2626']}
              style={styles.placeOrderGradient}
            >
              <Text style={styles.placeOrderText}>
                {placeOrderMutation.isPending ? 'Placing...' : `${side.toUpperCase()} ${selectedSymbol}`}
              </Text>
            </LinearGradient>
          </TouchableOpacity>
        </View>

        {/* Recent Orders */}
        <View style={[styles.ordersCard, { backgroundColor: theme.card }]}>
          <View style={styles.ordersHeader}>
            <Text style={[styles.sectionTitle, { color: theme.text }]}>Recent Orders</Text>
            <TouchableOpacity>
              <Text style={[styles.viewAll, { color: theme.primary }]}>View All</Text>
            </TouchableOpacity>
          </View>

          {mockOrders.map((order) => (
            <View
              key={order.id}
              style={[styles.orderItem, { borderBottomColor: theme.border }]}
            >
              <View style={styles.orderLeft}>
                <Text style={[styles.orderSymbol, { color: theme.text }]}>
                  {order.symbol}
                </Text>
                <Text style={[styles.orderType, { color: theme.textSecondary }]}>
                  {order.side.toUpperCase()}
                </Text>
              </View>

              <View style={styles.orderCenter}>
                <Text style={[styles.orderQuantity, { color: theme.text }]}>
                  {order.quantity} shares
                </Text>
                <Text style={[styles.orderPrice, { color: theme.textSecondary }]}>
                  @ ${order.price}
                </Text>
              </View>

              <View style={styles.orderRight}>
                <Text
                  style={[
                    styles.orderStatus,
                    {
                      color: order.status === 'filled' ? '#10b981' : '#f59e0b',
                      backgroundColor: order.status === 'filled' ? 'rgba(16, 185, 129, 0.1)' : 'rgba(245, 158, 11, 0.1)',
                    },
                  ]}
                >
                  {order.status}
                </Text>
              </View>
            </View>
          ))}
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
  searchButton: {
    padding: 8,
  },
  searchIcon: {
    fontSize: 20,
  },
  watchlist: {
    paddingLeft: 20,
  },
  watchlistContent: {
    paddingRight: 20,
  },
  watchlistItem: {
    padding: 16,
    marginRight: 12,
    borderRadius: 12,
    minWidth: 80,
    alignItems: 'center',
  },
  watchlistSymbol: {
    fontSize: 14,
    fontWeight: '600',
    marginBottom: 4,
  },
  watchlistPrice: {
    fontSize: 12,
    marginBottom: 2,
  },
  watchlistChange: {
    fontSize: 11,
    fontWeight: '500',
  },
  stockCard: {
    margin: 20,
    padding: 20,
    borderRadius: 20,
    elevation: 5,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  stockHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 20,
  },
  stockSymbol: {
    color: '#fff',
    fontSize: 24,
    fontWeight: 'bold',
  },
  stockCompany: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 14,
  },
  stockPriceContainer: {
    alignItems: 'flex-end',
  },
  stockPrice: {
    color: '#fff',
    fontSize: 28,
    fontWeight: 'bold',
  },
  stockChange: {
    color: '#10b981',
    fontSize: 14,
    fontWeight: '500',
  },
  stockStats: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  stockStat: {
    alignItems: 'center',
  },
  stockStatLabel: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 12,
    marginBottom: 4,
  },
  stockStatValue: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
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
  orderCard: {
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
  orderTypeContainer: {
    flexDirection: 'row',
    marginBottom: 20,
    gap: 8,
  },
  orderTypeButton: {
    flex: 1,
    paddingVertical: 12,
    borderRadius: 8,
    alignItems: 'center',
  },
  orderTypeText: {
    fontSize: 14,
    fontWeight: '600',
  },
  inputContainer: {
    marginBottom: 16,
  },
  inputLabel: {
    fontSize: 14,
    fontWeight: '500',
    marginBottom: 8,
  },
  input: {
    height: 48,
    borderRadius: 8,
    paddingHorizontal: 12,
    fontSize: 16,
  },
  orderSummary: {
    padding: 16,
    borderRadius: 8,
    marginBottom: 20,
  },
  summaryRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  summaryLabel: {
    fontSize: 14,
  },
  summaryValue: {
    fontSize: 16,
    fontWeight: '600',
  },
  placeOrderButton: {
    marginBottom: 10,
  },
  placeOrderGradient: {
    paddingVertical: 16,
    borderRadius: 12,
    alignItems: 'center',
  },
  placeOrderText: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '600',
  },
  ordersCard: {
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
  ordersHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 15,
  },
  viewAll: {
    fontSize: 14,
    fontWeight: '500',
  },
  orderItem: {
    flexDirection: 'row',
    paddingVertical: 12,
    borderBottomWidth: 1,
  },
  orderLeft: {
    flex: 1,
  },
  orderSymbol: {
    fontSize: 14,
    fontWeight: '600',
    marginBottom: 2,
  },
  orderType: {
    fontSize: 12,
  },
  orderCenter: {
    flex: 1,
    alignItems: 'center',
  },
  orderQuantity: {
    fontSize: 14,
    fontWeight: '500',
    marginBottom: 2,
  },
  orderPrice: {
    fontSize: 12,
  },
  orderRight: {
    flex: 1,
    alignItems: 'flex-end',
    justifyContent: 'center',
  },
  orderStatus: {
    fontSize: 12,
    fontWeight: '500',
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 12,
    overflow: 'hidden',
    textAlign: 'center',
  },
});