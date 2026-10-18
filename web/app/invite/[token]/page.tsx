import type { Metadata } from "next";
import { InviteView } from "@/components/organization/invite-view";

export const metadata: Metadata = { title: "Join organization" };

export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <InviteView token={token} />;
}
