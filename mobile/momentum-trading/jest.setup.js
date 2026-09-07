/* eslint-disable @typescript-eslint/no-var-requires */
/**
 * Test setup for the native client.
 *
 * Only modules that cannot run under jsdom are mocked here: chart rendering
 * (react-native-svg based) and gradients. Application code is never mocked —
 * these tests exist to assert what the screens actually render, so stubbing a
 * screen's own logic would defeat them.
 */
// @testing-library/react-native v13 registers its matchers automatically.

// AsyncStorage is a native module; the package ships an official jest mock.
jest.mock('@react-native-async-storage/async-storage', () =>
  require('@react-native-async-storage/async-storage/jest/async-storage-mock'),
);

jest.mock('react-native-chart-kit', () => {
  const React = require('react');
  const { Text } = require('react-native');
  const stub = (name) => (props) =>
    React.createElement(Text, { testID: name }, JSON.stringify(props.data));
  return {
    LineChart: stub('line-chart'),
    PieChart: stub('pie-chart'),
    BarChart: stub('bar-chart'),
  };
});

jest.mock('expo-linear-gradient', () => {
  const React = require('react');
  const { View } = require('react-native');
  return {
    LinearGradient: ({ children, ...rest }) =>
      React.createElement(View, rest, children),
  };
});

jest.mock('react-native-safe-area-context', () => {
  const React = require('react');
  const { View } = require('react-native');
  return {
    SafeAreaView: ({ children, ...rest }) =>
      React.createElement(View, rest, children),
    SafeAreaProvider: ({ children }) => children,
    useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
  };
});
