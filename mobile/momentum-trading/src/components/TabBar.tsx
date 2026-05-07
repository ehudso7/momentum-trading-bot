import React from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Dimensions,
} from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { BottomTabBarProps } from '@react-navigation/bottom-tabs';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useTheme } from '../contexts/ThemeContext';

const { width } = Dimensions.get('window');

interface TabItem {
  name: string;
  icon: string;
  activeIcon?: string;
}

const tabs: TabItem[] = [
  { name: 'Home', icon: '🏠', activeIcon: '🏠' },
  { name: 'Portfolio', icon: '💼', activeIcon: '💼' },
  { name: 'Trade', icon: '📈', activeIcon: '📈' },
  { name: 'Signals', icon: '🎯', activeIcon: '🎯' },
  { name: 'Profile', icon: '👤', activeIcon: '👤' },
];

export default function TabBar({ state, descriptors, navigation }: BottomTabBarProps) {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();

  return (
    <View
      style={[
        styles.container,
        {
          paddingBottom: Math.max(insets.bottom, 20),
          backgroundColor: theme.card,
          borderTopColor: theme.border,
        },
      ]}
    >
      <LinearGradient
        colors={[theme.card, theme.background]}
        style={StyleSheet.absoluteFill}
      />

      <View style={styles.tabsContainer}>
        {state.routes.map((route, index) => {
          const { options } = descriptors[route.key];
          const label = options.tabBarLabel ?? options.title ?? route.name;
          const isFocused = state.index === index;
          const tab = tabs.find((t) => t.name === route.name);

          const onPress = () => {
            const event = navigation.emit({
              type: 'tabPress',
              target: route.key,
              canPreventDefault: true,
            });

            if (!isFocused && !event.defaultPrevented) {
              navigation.navigate(route.name);
            }
          };

          const onLongPress = () => {
            navigation.emit({
              type: 'tabLongPress',
              target: route.key,
            });
          };

          return (
            <TouchableOpacity
              key={route.key}
              accessibilityRole="button"
              accessibilityState={isFocused ? { selected: true } : {}}
              accessibilityLabel={options.tabBarAccessibilityLabel}
              testID={options.tabBarTestID}
              onPress={onPress}
              onLongPress={onLongPress}
              style={[styles.tab, { flex: 1 }]}
              activeOpacity={0.7}
            >
              {isFocused && (
                <LinearGradient
                  colors={['rgba(102, 126, 234, 0.15)', 'transparent']}
                  style={styles.activeBackground}
                />
              )}

              <View style={[styles.iconContainer, isFocused && styles.iconContainerActive]}>
                {isFocused && (
                  <LinearGradient
                    colors={['#667eea', '#764ba2']}
                    style={styles.activeIconBg}
                  />
                )}

                <Text style={[styles.icon, isFocused && styles.iconActive]}>
                  {tab?.icon || '📱'}
                </Text>
              </View>

              <Text
                style={[
                  styles.label,
                  {
                    color: isFocused ? theme.primary : theme.textSecondary,
                    fontWeight: isFocused ? '600' : '400',
                  },
                ]}
              >
                {typeof label === 'string' ? label : route.name}
              </Text>

              {/* Active indicator dot */}
              {isFocused && <View style={styles.activeDot} />}
            </TouchableOpacity>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    borderTopWidth: 1,
    elevation: 8,
    shadowColor: '#000',
    shadowOffset: {
      width: 0,
      height: -2,
    },
    shadowOpacity: 0.1,
    shadowRadius: 8,
  },
  tabsContainer: {
    flexDirection: 'row',
    paddingTop: 10,
    paddingHorizontal: 10,
  },
  tab: {
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 8,
    position: 'relative',
  },
  activeBackground: {
    position: 'absolute',
    top: -10,
    left: -10,
    right: -10,
    bottom: -10,
    borderRadius: 20,
  },
  iconContainer: {
    position: 'relative',
    marginBottom: 4,
    alignItems: 'center',
    justifyContent: 'center',
    width: 40,
    height: 40,
    borderRadius: 20,
  },
  iconContainerActive: {
    elevation: 3,
    shadowColor: '#667eea',
    shadowOffset: {
      width: 0,
      height: 2,
    },
    shadowOpacity: 0.25,
    shadowRadius: 3.84,
  },
  activeIconBg: {
    position: 'absolute',
    width: 36,
    height: 36,
    borderRadius: 18,
  },
  icon: {
    fontSize: 20,
    zIndex: 1,
  },
  iconActive: {
    color: '#fff',
  },
  label: {
    fontSize: 11,
    textAlign: 'center',
    marginTop: 2,
  },
  activeDot: {
    position: 'absolute',
    bottom: -8,
    width: 4,
    height: 4,
    borderRadius: 2,
    backgroundColor: '#667eea',
  },
});