import "server-only";

import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { isOwnerEmail, isPrivateMode, ownerEmails } from "@/lib/access-policy";

export type OwnerAuthorization =
  | { ok: true; userId: string; email: string }
  | { ok: false; status: 401 | 403 | 503; error: string };

export async function authorizeOwner(): Promise<OwnerAuthorization> {
  if (
    !process.env.NEXT_PUBLIC_SUPABASE_URL ||
    !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  ) {
    return { ok: false, status: 503, error: "Owner authentication is not configured." };
  }
  if (isPrivateMode() && ownerEmails().size === 0) {
    return { ok: false, status: 503, error: "The private owner allow-list is not configured." };
  }

  const supabase = await createClient();
  const {
    data: { user },
    error,
  } = await supabase.auth.getUser();

  if (error || !user) {
    return { ok: false, status: 401, error: "Authentication required." };
  }
  if (isPrivateMode() && !isOwnerEmail(user.email)) {
    return { ok: false, status: 403, error: "This account is not authorized." };
  }
  if (!user.email) {
    return { ok: false, status: 403, error: "The authenticated account has no email." };
  }
  return { ok: true, userId: user.id, email: user.email };
}

export async function requireOwnerPage(): Promise<{ userId: string; email: string }> {
  const result = await authorizeOwner();
  if (!result.ok) {
    const code =
      result.status === 503
        ? "owner_not_configured"
        : result.status === 403
          ? "unauthorized"
          : "login_required";
    redirect(`/login?error=${code}`);
  }
  return { userId: result.userId, email: result.email };
}
