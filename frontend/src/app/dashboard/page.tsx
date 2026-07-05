import { redirect } from "next/navigation";

export default async function DashboardRedirect({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { checkout } = await searchParams;
  if (checkout === "success") {
    redirect("/billing?checkout=success");
  }
  if (checkout === "cancel") {
    redirect("/billing?checkout=cancel");
  }
  // The live trading dashboard lives at the root route
  redirect("/");
}
