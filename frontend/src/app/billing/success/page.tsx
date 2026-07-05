import Link from "next/link";
import { CheckCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CheckoutConfirmation } from "@/components/billing/checkout-confirmation";

export default function BillingSuccessPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#0a0b14] px-4">
      <Card className="w-full max-w-md text-center">
        <CardHeader>
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-emerald-500/20">
            <CheckCircle className="h-8 w-8 text-emerald-400" />
          </div>
          <CardTitle>Payment received</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <CheckoutConfirmation />
          <p className="text-sm text-zinc-400">
            Premium signals and full scanner access unlock as soon as your
            upgrade is confirmed.
          </p>
          <Link href="/">
            <Button className="w-full">Go to Dashboard</Button>
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
