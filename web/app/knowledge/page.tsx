import type { Metadata } from "next";
import { KnowledgeView } from "@/components/knowledge/knowledge-view";

export const metadata: Metadata = { title: "Knowledge Base" };

export default function KnowledgePage() {
  return <KnowledgeView />;
}
