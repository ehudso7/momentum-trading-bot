import { DashboardClient } from "@/components/dashboard/dashboard-client";
import { requireOwnerPage } from "@/lib/owner-access";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const owner = await requireOwnerPage();
  return <DashboardClient userEmail={owner.email} />;
}
