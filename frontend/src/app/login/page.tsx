import { AuthForm } from "@/components/auth/auth-form";
import { isPrivateMode } from "@/lib/access-policy";

const ERRORS: Record<string, string> = {
  login_required: "Sign in with the private owner account to continue.",
  unauthorized: "This account is not on the private owner allow-list.",
  owner_not_configured: "The private owner allow-list has not been configured.",
  owner_authentication_not_configured: "Owner authentication is not configured on this deployment.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  return (
    <AuthForm
      mode="login"
      privateMode={isPrivateMode()}
      initialError={error ? ERRORS[error] ?? "Access could not be verified." : null}
    />
  );
}
