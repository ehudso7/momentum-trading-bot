/** Pure private-access policy helpers shared by Proxy and server routes. */

export function isPrivateMode(): boolean {
  const configured = process.env.TRADING_PRIVATE_MODE?.trim().toLowerCase();
  if (configured === "false" || configured === "0" || configured === "off") {
    return false;
  }
  if (configured === "true" || configured === "1" || configured === "on") {
    return true;
  }
  // A production deployment must fail private by default. Public launch is an
  // explicit future decision, never an accidental missing environment value.
  return process.env.NODE_ENV === "production";
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
