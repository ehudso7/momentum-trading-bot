import React, { useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  Alert,
  Switch,
} from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { useQuery } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { api } from '../services/api';

export default function ProfileScreen() {
  const { theme, isDark, toggleTheme } = useTheme();
  const { user, logout } = useAuth();
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [biometricEnabled, setBiometricEnabled] = useState(true);

  const { data: subscription } = useQuery({
    queryKey: ['subscription'],
    queryFn: api.getSubscription,
  });

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const handleLogout = () => {
    Alert.alert(
      'Confirm Logout',
      'Are you sure you want to logout?',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Logout',
          style: 'destructive',
          onPress: async () => {
            await logout();
          },
        },
      ]
    );
  };

  const handleUpgrade = async () => {
    try {
      const checkoutSession = await api.createCheckoutSession('price_1TQC0vBVIDu5AoABCJOLlQID');
      // In real app, open checkout URL
      Alert.alert('Upgrade', 'Redirecting to payment...');
    } catch (error) {
      Alert.alert('Error', 'Failed to start upgrade process');
    }
  };

  const menuItems = [
    {
      section: 'Account',
      items: [
        { icon: '👤', title: 'Personal Information', onPress: () => {} },
        { icon: '🔒', title: 'Security & Privacy', onPress: () => {} },
        { icon: '💳', title: 'Billing & Subscription', onPress: () => {} },
        { icon: '📊', title: 'Trading Preferences', onPress: () => {} },
      ],
    },
    {
      section: 'App Settings',
      items: [
        {
          icon: '🔔',
          title: 'Push Notifications',
          onPress: () => setNotificationsEnabled(!notificationsEnabled),
          rightComponent: (
            <Switch
              value={notificationsEnabled}
              onValueChange={setNotificationsEnabled}
              trackColor={{ false: theme.border, true: theme.primary }}
              thumbColor={notificationsEnabled ? '#fff' : '#f4f3f4'}
            />
          ),
        },
        {
          icon: '🌙',
          title: 'Dark Mode',
          onPress: toggleTheme,
          rightComponent: (
            <Switch
              value={isDark}
              onValueChange={toggleTheme}
              trackColor={{ false: theme.border, true: theme.primary }}
              thumbColor={isDark ? '#fff' : '#f4f3f4'}
            />
          ),
        },
        {
          icon: '🔐',
          title: 'Biometric Authentication',
          onPress: () => setBiometricEnabled(!biometricEnabled),
          rightComponent: (
            <Switch
              value={biometricEnabled}
              onValueChange={setBiometricEnabled}
              trackColor={{ false: theme.border, true: theme.primary }}
              thumbColor={biometricEnabled ? '#fff' : '#f4f3f4'}
            />
          ),
        },
        { icon: '📱', title: 'App Version', subtitle: 'v1.0.0 (Build 1)', onPress: () => {} },
      ],
    },
    {
      section: 'Support',
      items: [
        { icon: '❓', title: 'Help Center', onPress: () => {} },
        { icon: '💬', title: 'Contact Support', onPress: () => {} },
        { icon: '📖', title: 'Terms of Service', onPress: () => {} },
        { icon: '🛡️', title: 'Privacy Policy', onPress: () => {} },
        { icon: '⭐', title: 'Rate App', onPress: () => {} },
      ],
    },
  ];

  return (
    <SafeAreaView style={[styles.container, { backgroundColor: theme.background }]}>
      <ScrollView showsVerticalScrollIndicator={false}>
        {/* Profile Header */}
        <LinearGradient
          colors={['#667eea', '#764ba2']}
          style={styles.profileHeader}
        >
          <View style={styles.avatarContainer}>
            <LinearGradient
              colors={['#fff', '#f0f0f0']}
              style={styles.avatar}
            >
              <Text style={styles.avatarText}>
                {user?.name?.charAt(0)?.toUpperCase() || 'U'}
              </Text>
            </LinearGradient>
          </View>

          <Text style={styles.userName}>{user?.name || 'User'}</Text>
          <Text style={styles.userEmail}>{user?.email || 'user@example.com'}</Text>

          <View style={styles.subscriptionBadge}>
            <Text style={styles.subscriptionText}>
              {user?.tier?.toUpperCase() || 'FREE'} MEMBER
            </Text>
          </View>
        </LinearGradient>

        {/* Stats Cards */}
        <View style={styles.statsContainer}>
          <View style={[styles.statCard, { backgroundColor: theme.card }]}>
            <Text style={[styles.statValue, { color: theme.text }]}>$12,450</Text>
            <Text style={[styles.statLabel, { color: theme.textSecondary }]}>
              Total Gains
            </Text>
          </View>

          <View style={[styles.statCard, { backgroundColor: theme.card }]}>
            <Text style={[styles.statValue, { color: theme.text }]}>156</Text>
            <Text style={[styles.statLabel, { color: theme.textSecondary }]}>
              Trades Won
            </Text>
          </View>

          <View style={[styles.statCard, { backgroundColor: theme.card }]}>
            <Text style={[styles.statValue, { color: theme.text }]}>87%</Text>
            <Text style={[styles.statLabel, { color: theme.textSecondary }]}>
              Win Rate
            </Text>
          </View>
        </View>

        {/* Upgrade Card */}
        {user?.tier === 'free' && (
          <TouchableOpacity onPress={handleUpgrade} style={styles.upgradeCardContainer}>
            <LinearGradient
              colors={['#10b981', '#059669']}
              style={styles.upgradeCard}
            >
              <Text style={styles.upgradeTitle}>🚀 Upgrade to Premium</Text>
              <Text style={styles.upgradeSubtitle}>
                Unlock unlimited AI signals, advanced analytics, and more
              </Text>
              <View style={styles.upgradeFeatures}>
                <Text style={styles.upgradeFeature}>✓ Unlimited trading signals</Text>
                <Text style={styles.upgradeFeature}>✓ Real-time portfolio analytics</Text>
                <Text style={styles.upgradeFeature}>✓ Priority customer support</Text>
              </View>
              <View style={styles.upgradePrice}>
                <Text style={styles.upgradePriceText}>$29.99/month</Text>
              </View>
            </LinearGradient>
          </TouchableOpacity>
        )}

        {/* Menu Sections */}
        {menuItems.map((section, sectionIndex) => (
          <View key={sectionIndex} style={styles.menuSection}>
            <Text style={[styles.sectionTitle, { color: theme.text }]}>
              {section.section}
            </Text>

            <View style={[styles.menuCard, { backgroundColor: theme.card }]}>
              {section.items.map((item, itemIndex) => (
                <TouchableOpacity
                  key={itemIndex}
                  style={[
                    styles.menuItem,
                    {
                      borderBottomColor: theme.border,
                      borderBottomWidth: itemIndex === section.items.length - 1 ? 0 : 1,
                    },
                  ]}
                  onPress={item.onPress}
                >
                  <View style={styles.menuItemLeft}>
                    <Text style={styles.menuIcon}>{item.icon}</Text>
                    <View style={styles.menuItemContent}>
                      <Text style={[styles.menuItemTitle, { color: theme.text }]}>
                        {item.title}
                      </Text>
                      {item.subtitle && (
                        <Text style={[styles.menuItemSubtitle, { color: theme.textSecondary }]}>
                          {item.subtitle}
                        </Text>
                      )}
                    </View>
                  </View>

                  <View style={styles.menuItemRight}>
                    {item.rightComponent || (
                      <Text style={[styles.menuArrow, { color: theme.textSecondary }]}>
                        ›
                      </Text>
                    )}
                  </View>
                </TouchableOpacity>
              ))}
            </View>
          </View>
        ))}

        {/* Logout Button */}
        <TouchableOpacity onPress={handleLogout} style={styles.logoutButton}>
          <Text style={[styles.logoutText, { color: '#ef4444' }]}>
            🔓 Logout
          </Text>
        </TouchableOpacity>

        {/* App Info */}
        <View style={styles.appInfo}>
          <Text style={[styles.appInfoText, { color: theme.textSecondary }]}>
            Momentum Trading Pro v1.0.0
          </Text>
          <Text style={[styles.appInfoText, { color: theme.textSecondary }]}>
            © 2024 Momentum Trading Pro
          </Text>
        </View>

        <View style={{ height: 50 }} />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  profileHeader: {
    alignItems: 'center',
    paddingVertical: 40,
    paddingHorizontal: 20,
  },
  avatarContainer: {
    marginBottom: 16,
  },
  avatar: {
    width: 80,
    height: 80,
    borderRadius: 40,
    alignItems: 'center',
    justifyContent: 'center',
    elevation: 3,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  avatarText: {
    fontSize: 32,
    fontWeight: 'bold',
    color: '#667eea',
  },
  userName: {
    color: '#fff',
    fontSize: 24,
    fontWeight: 'bold',
    marginBottom: 4,
  },
  userEmail: {
    color: 'rgba(255, 255, 255, 0.8)',
    fontSize: 16,
    marginBottom: 16,
  },
  subscriptionBadge: {
    backgroundColor: 'rgba(255, 255, 255, 0.2)',
    paddingHorizontal: 16,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: 'rgba(255, 255, 255, 0.3)',
  },
  subscriptionText: {
    color: '#fff',
    fontSize: 12,
    fontWeight: '600',
  },
  statsContainer: {
    flexDirection: 'row',
    paddingHorizontal: 20,
    marginTop: -20,
    gap: 10,
  },
  statCard: {
    flex: 1,
    padding: 16,
    borderRadius: 12,
    alignItems: 'center',
    elevation: 2,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.22,
    shadowRadius: 2.22,
  },
  statValue: {
    fontSize: 20,
    fontWeight: 'bold',
    marginBottom: 4,
  },
  statLabel: {
    fontSize: 12,
    textAlign: 'center',
  },
  upgradeCardContainer: {
    margin: 20,
    marginTop: 20,
  },
  upgradeCard: {
    padding: 20,
    borderRadius: 16,
    elevation: 3,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  upgradeTitle: {
    color: '#fff',
    fontSize: 18,
    fontWeight: 'bold',
    marginBottom: 8,
  },
  upgradeSubtitle: {
    color: 'rgba(255, 255, 255, 0.9)',
    fontSize: 14,
    marginBottom: 16,
  },
  upgradeFeatures: {
    marginBottom: 16,
  },
  upgradeFeature: {
    color: 'rgba(255, 255, 255, 0.9)',
    fontSize: 13,
    marginBottom: 4,
  },
  upgradePrice: {
    alignItems: 'center',
  },
  upgradePriceText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: 'bold',
  },
  menuSection: {
    marginTop: 20,
    paddingHorizontal: 20,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 12,
  },
  menuCard: {
    borderRadius: 12,
    elevation: 1,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.18,
    shadowRadius: 1.0,
  },
  menuItem: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 16,
    paddingHorizontal: 16,
  },
  menuItemLeft: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
  },
  menuIcon: {
    fontSize: 20,
    marginRight: 16,
  },
  menuItemContent: {
    flex: 1,
  },
  menuItemTitle: {
    fontSize: 16,
    fontWeight: '500',
    marginBottom: 2,
  },
  menuItemSubtitle: {
    fontSize: 12,
  },
  menuItemRight: {
    alignItems: 'center',
    justifyContent: 'center',
  },
  menuArrow: {
    fontSize: 20,
    fontWeight: '300',
  },
  logoutButton: {
    margin: 20,
    padding: 16,
    alignItems: 'center',
  },
  logoutText: {
    fontSize: 16,
    fontWeight: '600',
  },
  appInfo: {
    alignItems: 'center',
    paddingHorizontal: 20,
  },
  appInfoText: {
    fontSize: 12,
    marginBottom: 4,
  },
});