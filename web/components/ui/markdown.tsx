import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * A deliberately small Markdown renderer for trusted, server-authored text:
 * the legal documents and the compiled dossier.
 *
 * It handles headings, lists, tables, bold, inline code and paragraphs, and
 * nothing else. Pulling a full Markdown library plus a sanitiser in to render
 * two files we wrote ourselves would be more dependency than the job needs;
 * anything user-authored gets escaped by React before it reaches here.
 */
function inline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      parts.push(
        <code key={key++} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
          {token.slice(1, -1)}
        </code>,
      );
    } else {
      const label = token.slice(1, token.indexOf("]"));
      const href = token.slice(token.indexOf("(") + 1, -1);
      parts.push(
        <a key={key++} href={href} className="underline hover:text-foreground">
          {label}
        </a>,
      );
    }
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function Markdown({ children, className }: { children: string; className?: string }) {
  const blocks: React.ReactNode[] = [];
  const lines = children.split("\n");
  let list: string[] = [];
  let table: string[] = [];
  let key = 0;

  const flushList = () => {
    if (!list.length) return;
    blocks.push(
      <ul key={key++} className="my-3 list-disc space-y-1 pl-5">
        {list.map((item, i) => (
          <li key={i}>{inline(item)}</li>
        ))}
      </ul>,
    );
    list = [];
  };
  const flushTable = () => {
    if (table.length < 2) {
      table = [];
      return;
    }
    const cells = (row: string) =>
      row.split("|").slice(1, -1).map((c) => c.trim());
    const header = cells(table[0]);
    const body = table.slice(2).map(cells);
    blocks.push(
      <div key={key++} className="my-4 overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-border">
              {header.map((cell, i) => (
                <th key={i} className="px-2 py-1.5 text-left font-medium">{inline(cell)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, i) => (
              <tr key={i} className="border-b border-border/60">
                {row.map((cell, j) => (
                  <td key={j} className="px-2 py-1.5 align-top text-muted-foreground">{inline(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
    table = [];
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith("|")) {
      flushList();
      table.push(line);
      continue;
    }
    flushTable();
    if (/^[-*] /.test(line)) {
      list.push(line.slice(2));
      continue;
    }
    flushList();
    if (!line.trim()) continue;
    if (line.startsWith("### ")) {
      blocks.push(<h3 key={key++} className="mt-5 text-sm font-semibold">{inline(line.slice(4))}</h3>);
    } else if (line.startsWith("## ")) {
      blocks.push(<h2 key={key++} className="mt-6 text-base font-semibold tracking-tight">{inline(line.slice(3))}</h2>);
    } else if (line.startsWith("# ")) {
      blocks.push(<h1 key={key++} className="mt-2 text-xl font-semibold tracking-tight">{inline(line.slice(2))}</h1>);
    } else if (line.startsWith("---")) {
      blocks.push(<hr key={key++} className="my-5 border-border" />);
    } else {
      blocks.push(<p key={key++} className="my-3 leading-relaxed">{inline(line)}</p>);
    }
  }
  flushList();
  flushTable();
  return <div className={cn("text-sm text-muted-foreground", className)}>{blocks}</div>;
}
