/**
 * Jest configuration for the native client.
 *
 * `jest-expo` supplies the React Native preset (Babel transform, module
 * mapping, and the native-module mocks Expo ships). `transformIgnorePatterns`
 * has to allow the RN/Expo packages through Babel because they publish
 * untranspiled ESM.
 */
module.exports = {
  preset: 'jest-expo',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
  testMatch: ['<rootDir>/src/**/__tests__/**/*.test.{ts,tsx}'],
  collectCoverageFrom: ['src/**/*.{ts,tsx}', '!src/**/__tests__/**'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@sentry/react-native|native-base|react-native-svg|react-native-chart-kit)',
  ],
};
