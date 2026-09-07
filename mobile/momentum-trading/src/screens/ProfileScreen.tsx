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
import { SafeAreaView } from 'react-native-safe-area-context';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { api } from '../services/api';
import DataUnavailable from '../components/DataUnavailable';

// Declared explicitly so the heterogeneous entries below (some carry a
// subtitle, some a trailing control, some neither) infer as one item type
// rather than a union TypeScript cannot narrow at the render site.
interface MenuItem {
  icon: string;
  title: string;
  onPress: () => void;
  subtitle?: string;
  rightComponent?: React.ReactNode;
}

interface MenuSection {
  section: string;
  items: MenuItem[];
}

export default function ProfileScreen() {
  const { theme, isDark, toggleTheme } = useTheme();
  const { user, logout } = useAuth();
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [biometricEnabled, setBiometricEnabled] = useState(true);

  // The /subscription and /settings queries used to run here and their
  // results were never rendered — the same dead-request defect as the
  // /performance query on PortfolioScreen. Re-add them alongside the UI that
  // actually displays them.

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
      await api.createCheckoutSession('price_1TQC0vBVIDu5AoABCJOLlQID');
      // In real app, open checkout URL
      Alert.alert('Upgrade', 'Redirecting to payment...');
    } catch (error) {
      Alert.alert('Error', 'Failed to start upgrade process');
    }
  };

  const menuItems: MenuSection[] = [
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

        {/* Lifetime performance stats.

            Previously hardcoded to "$12,450 / 156 / 87%" for every account,
            which read as the signed-in user's own trading record. No endpoint
            publishes per-user performance, so there is nothing to show. Wire
            this to a real stats endpoint before restoring the three cards. */}
        <View style={styles.statsContainer}>
          <DataUnavailable
            title="Performance stats unavailable"
            detail="Your trading statistics are not published yet."
          />
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