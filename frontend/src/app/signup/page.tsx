import { Suspense } from "react";
import { AuthForm } from "@/components/auth/auth-form";
import { RefCapture } from "@/components/share/ref-capture";

export default function SignupPage() {
  return (
    <>
      {/* Viral loop: persist ?ref=<token> from shared signal cards
          (first touch) so the referral survives the auth redirect. */}
      <Suspense fallback={null}>
        <RefCapture />
      </Suspense>
      <AuthForm mode="signup" />
    </>
  );
}