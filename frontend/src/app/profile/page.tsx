import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";
import Link from "next/link";
import { ArrowLeft, Key, User } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { createClient } from "@/lib/supabase/server";
import { ApiKeyForm } from "@/components/profile/api-key-form";

export default async function ProfilePage() {
  if (
    !process.env.NEXT_PUBLIC_SUPABASE_URL ||
    !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  ) {
    redirect("/");
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) redirect("/login");

  const { data: trades } = await supabase
    .from("trades")
    .select("*")
    .eq("user_id", user.id)
    .order("created_at", { ascending: false })
    .limit(10);

  return (
    <div className="min-h-screen bg-[#0a0b14]">
      <div className="relative mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <Link
          href="/"
          className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to dashboard
        </Link>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-5 w-5 text-cyan-400" />
              Profile
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <p className="text-zinc-400">
              Email: <span className="text-white">{user.email}</span>
            </p>
            <p className="text-zinc-400">
              Member since:{" "}
              <span className="text-white">
                {new Date(user.created_at).toLocaleDateString()}
              </span>
            </p>
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Key className="h-5 w-5 text-violet-400" />
              API Key
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-4 text-sm text-zinc-400">
              Connect to your Railway backend for premium signal access.
            </p>
            <ApiKeyForm />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent Synced Trades</CardTitle>
          </CardHeader>
          <CardContent>
            {!trades?.length ? (
              <p className="text-sm text-zinc-500">No trades synced yet.</p>
            ) : (
              <div className="space-y-2">
                {trades.map((trade) => (
                  <div
                    key={trade.id}
                    className="flex items-center justify-between rounded-lg border border-white/5 bg-white/[0.02] p-3 text-sm"
                  >
                    <span className="font-medium text-white">{trade.symbol}</span>
                    <span className="text-zinc-400">{trade.side}</span>
                    <span className="text-zinc-400">{trade.qty}</span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}