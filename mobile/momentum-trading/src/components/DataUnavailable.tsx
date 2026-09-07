import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { useTheme } from '../contexts/ThemeContext';

/**
 * Placeholder shown wherever a screen has no real data to display.
 *
 * This exists to keep a rule the app previously broke: never render invented
 * numbers in a position where a user would read them as their own money.
 * Several screens used to fall back to hardcoded holdings, equity curves and
 * signal confidences when the API returned nothing, which is indistinguishable
 * from real data on screen. An explicit "unavailable" state is the honest
 * substitute — if you are tempted to replace this with sample figures, don't.
 */
export default function DataUnavailable({
  title = 'No data available',
  detail,
}: {
  title?: string;
  detail?: string;
}) {
  const { theme } = useTheme();

  return (
    <View style={styles.container}>
      <Text style={[styles.title, { color: theme.textSecondary }]}>{title}</Text>
      {detail ? (
        <Text style={[styles.detail, { color: theme.textSecondary }]}>{detail}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    paddingVertical: 28,
    paddingHorizontal: 16,
    alignItems: 'center',
    justifyContent: 'center',
  },
  title: {
    fontSize: 15,
    fontWeight: '600',
    textAlign: 'center',
  },
  detail: {
    marginTop: 6,
    fontSize: 13,
    lineHeight: 18,
    textAlign: 'center',
    opacity: 0.8,
  },
});
