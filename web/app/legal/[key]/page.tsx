import type { Metadata } from "next";
import { LegalDocumentView } from "@/components/legal/legal-document";

export const metadata: Metadata = { title: "Legal" };

export default async function LegalDocumentPage({ params }: { params: Promise<{ key: string }> }) {
  const { key } = await params;
  return <LegalDocumentView documentKey={key} />;
}
