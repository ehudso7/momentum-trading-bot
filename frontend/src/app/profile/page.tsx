export const dynamic = "force-dynamic";
import Link from "next/link";
import { ArrowLeft, LockKeyhole, User } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { requireOwnerPage } from "@/lib/owner-access";

export default async function ProfilePage() {
  const owner = await requireOwnerPage();

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
              Email: <span className="text-white">{owner.email}</span>
            </p>
            <p className="text-zinc-400">
              Access: <span className="text-white">Private owner</span>
            </p>
          </CardContent>
        </Card>

        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <LockKeyhole className="h-5 w-5 text-violet-400" />
              Credential Boundary
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-4 text-sm text-zinc-400">
              Railway credentials are attached only by the server-side proxy.
              They are not returned to this browser or stored in local storage.
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
