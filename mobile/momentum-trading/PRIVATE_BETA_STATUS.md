# Native Client Release Status

The native iOS/Android client is intentionally excluded from the private paper
launch. The validated operator surface is the private Next.js web application.

Native production builds and store submission remain blocked. Do not distribute
this directory or use it to place or represent orders.

Re-enable production profiles only after the public-product gate in
`docs/PRIVATE_PAPER_LAUNCH.md` has passed and the native client is covered by CI.

## Status of the previously listed blockers

The original blocker list was qualitative ("legacy fixture screens", "Expo
dependency alignment", ...). It has been re-derived against the actual tree so
each item is verifiable. Two are now closed; the rest are open with the evidence
that establishes them.

### Closed

- **Legacy fixture screens — CLOSED.** Every screen that rendered hardcoded
  money, holdings, quotes or performance has been converted to render from its
  real query with an explicit "unavailable" state (`src/components/DataUnavailable.tsx`).
  Removed: `PortfolioScreen` allocation pie / equity curve / `mockPositions` /
  `$112,450.73`; `HomeScreen` equity curve and `$105,000` fallback;
  `SignalsScreen` `mockSignals`, the "95.2% accuracy / +24.8% avg return"
  performance card, and the "AI Market Insights" block (BULLISH regime,
  "volatility to increase 15% due to FOMC", sector rotation);
  `TradingScreen` per-symbol `$150.25 / +2.5%` chips, "Apple Inc.",
  OHLC/volume constants and the sample price series; `ProfileScreen`
  "$12,450 total gains / 156 trades won / 87% win rate".
- **Expo dependency alignment — CLOSED.** 14 packages were pinned to versions
  that do not exist for SDK 54 (e.g. `expo-constants ^55.0.16`,
  `expo-notifications ^55.0.22`, `expo-updates ^55.0.21`). All are now aligned to
  the SDK 54 bundled versions, `react-native-worklets` was added as a required
  peer, and six declared-but-never-imported native modules were dropped
  (`victory-native`, `lottie-react-native`, `expo-blur`, `expo-haptics`,
  `expo-device`, `zustand`). `expo-doctor` improved from 14/18 to 16/18 checks
  passing; the two remaining failures are network reachability to Expo's schema
  and React Native directory endpoints, not project defects.

### Open — backend

- **The mobile API is gated off.** `trading_bot/dashboard/app.py:134` mounts the
  mobile router only when `TRADING_ENABLE_LEGACY_MOBILE_API` is truthy and
  private mode is off. In the shipped configuration none of the endpoints the
  app calls exist.
- **Path prefix mismatch.** The router is declared as
  `APIRouter(prefix="/api/mobile", ...)` in `trading_bot/api/mobile_routes.py`,
  but `src/services/api.ts` calls `/portfolio`, `/signals/latest`, `/market/...`
  with no prefix. Every call would 404 even with the router enabled. This was
  deliberately **not** patched: pointing the client at these endpoints is only
  worth doing once the two auth items below are fixed.
- **No token validation.** Every protected route depends on
  `security = HTTPBearer()`, which extracts a bearer token and never verifies
  it. Any non-empty string authenticates.
- **Hardcoded credential branch.** The login handler accepts
  `demo@example.com` / `demo123` and returns a token of the form
  `"demo_token_" + timestamp`.
- **Handlers return canned constants.** `get_market_data` returns
  `price: 150.25, change: 2.50, open: 148.25, ...` for every symbol; the
  portfolio handler returns fixed positions. Fixing the client alone does not
  produce real data — the backend fabricates it too. The client now renders
  whatever the endpoint returns, so it becomes correct when these handlers do.

Authentication and authorization are human-approval paths under `CLAUDE.md`
policy and `**/*auth*` is a sensitive path, so the auth contract is left for an
explicit decision rather than being designed here.

### Open — billing

`ProfileScreen.handleUpgrade` calls `api.createCheckoutSession` with a hardcoded
Stripe price id and then only shows an alert reading "Redirecting to payment...";
no checkout URL is opened. The card advertises `$29.99/month`. Left untouched:
billing is a human-approval path, and Apple requires in-app purchase for digital
subscriptions, so the Stripe flow is a store-review blocker on its own.

### Open — release engineering

- `eas.json` defines only `development` and `preview` profiles. There is no
  `production` profile and no `submit` section, so no TestFlight build can be
  produced from this configuration.
- `submit-to-stores.sh` is disabled at the top with `exit 64`.
- `app.json` carries `"releaseStatus": "quarantined-private-web-only"`.
- Signing credentials and an App Store Connect API key are required and are not
  present in the repository (correctly — they are secrets).

### Open — store and legal

Privacy disclosures (`NSUserTrackingUsageDescription`, privacy manifest), the
App Store privacy questionnaire, and Guideline 3.2.1 positioning review are
outstanding. `TradingScreen` already implements the broker hand-off pattern and
carries non-advice disclaimers, which is the intended posture.

## CI coverage

`.github/workflows/ci.yml` now runs a `mobile` job: `npm ci`, a blocking
`npm audit --omit=dev --audit-level=critical`, an informational high-severity
audit, a blocking `npm run typecheck`, a blocking `npm run test:ci`, and a
non-blocking `expo-doctor`.

The client previously had no test harness at all, which is why fabricated data
survived in five screens. `jest-expo` + `@testing-library/react-native` are now
configured, with 13 tests across `HomeScreen`, `PortfolioScreen`,
`SignalsScreen`, `TradingScreen` and `ProfileScreen`. Each screen is asserted
twice: that it renders the values its API actually returns, and that with an
empty or failing API it renders an explicit unavailable state containing none of
the previously hardcoded literals (`src/screens/__tests__/renderScreen.tsx`
holds that append-only list). The guards were verified by reintroducing a
fabricated literal and confirming the suite goes red.

The audit gate is set at critical rather than high deliberately. Every current
high advisory (`postcss`, `image-size`) reaches this project only through Expo's
build toolchain, and npm reports the only fix as `expo@57` — an SDK 54 to 57
major upgrade, which is a separate change. The informational step keeps those
advisories visible rather than silently accepted.

## TestFlight readiness

Not ready. The blockers above are ordered by dependency: the backend auth
contract gates the API work, the API work gates meaningful end-to-end testing,
and release engineering gates any build reaching TestFlight at all.
