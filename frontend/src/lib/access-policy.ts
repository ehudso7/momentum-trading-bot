/** Pure private-access policy helpers shared by Proxy and server routes. */

const TRUE_VALUES = new Set(["true", "1", "on", "yes"]);
const FALSE_VALUES = new Set(["false", "0", "off", "no"]);

function normalizedEnv(name: string): string {
  return process.env[name]?.trim().toLowerCase() ?? "";
}

export function isPrivateMode(): boolean {
  const privateMode = normalizedEnv("TRADING_PRIVATE_MODE");
  const publicProductEnabled = normalizedEnv(
    "TRADING_PUBLIC_PRODUCT_ENABLED"
  );

  if (TRUE_VALUES.has(privateMode)) {
    return true;
  }

  // Public mode is a two-key decision. A deployment only leaves private mode
  // when private mode is explicitly disabled AND the separate public-product
  // gate is explicitly enabled. Missing, malformed, or partially changed
  // configuration remains owner-only by default.
  const privateExplicitlyDisabled = FALSE_VALUES.has(privateMode);
  const publicExplicitlyEnabled = TRUE_VALUES.has(publicProductEnabled);

  return !(privateExplicitlyDisabled && publicExplicitlyEnabled);
}

export function ownerEmails(): Set<string> {
  return new Set(
    (process.env.TRADING_PRIVATE_OWNER_EMAILS ?? "")
      .split(",")
      .map((email) => email.trim().toLowerCase())
      .filter(Boolean)
  );
}

export function isOwnerEmail(email: string | null | undefined): boolean {
  if (!email) return false;
  return ownerEmails().has(email.trim().toLowerCase());
}

export function isPublicPrivateModePath(pathname: string): boolean {
  return (
    pathname === "/login" ||
    pathname === "/privacy" ||
    pathname === "/terms" ||
    pathname.startsWith("/auth/callback")
  );
}
