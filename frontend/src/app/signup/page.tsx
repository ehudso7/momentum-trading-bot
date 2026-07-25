import { Suspense } from "react";
import { redirect } from "next/navigation";
import { AuthForm } from "@/components/auth/auth-form";
import { RefCapture } from "@/components/share/ref-capture";
import { isPrivateMode } from "@/lib/access-policy";

export default function SignupPage() {
  if (isPrivateMode()) redirect("/login");
  return (
    <>
      <Suspense fallback={null}>
        <RefCapture />
      </Suspense>
      <AuthForm mode="signup" />
    </>
  );
}
