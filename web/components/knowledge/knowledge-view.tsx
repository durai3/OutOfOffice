"use client";

import * as React from "react";
import { AlertTriangle, Database, FileText, FolderSync, Layers, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { ChunkPage, FileIngestResult, IndexedDocument, KnowledgeOverview, RetrievalResult } from "@/lib/types";
import { cn, formatBytes, formatDateTime } from "@/lib/utils";
import { useApi } from "@/hooks/use-api";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/page-header";

export function KnowledgeView() {
  const overview = useApi<KnowledgeOverview>(api.knowledge, { refreshMs: 20_000 });
  const data = overview.data;
  const [chunksFor, setChunksFor] = React.useState<IndexedDocument | null>(null);
  const [removing, setRemoving] = React.useState<IndexedDocument | null>(null);
  const toast = useToast();

  return (
    <>
      <PageHeader
        title="Knowledge Base"
        description="Documents the delegate may quote from. Every answer is lifted verbatim from a passage that clears the similarity threshold."
        actions={
          <Button variant="outline" size="sm" onClick={() => void overview.refresh()}>
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
        }
      />

      {overview.error && !data ? <div className="mb-4 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">{overview.error}</div> : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <HealthCard data={data} />
        <div className="lg:col-span-2">
          <UploadCard data={data} onDone={() => void overview.refresh()} />
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <DocumentsCard data={data} onChunks={setChunksFor} onRemove={setRemoving} onChanged={() => void overview.refresh()} />
        </div>
        <RetrievalTester data={data} />
      </div>

      <ChunksDialog document={chunksFor} onClose={() => setChunksFor(null)} />

      <Dialog
        open={Boolean(removing)}
        onClose={() => setRemoving(null)}
        title="Remove this document?"
        footer={
          <>
            <Button variant="outline" onClick={() => setRemoving(null)}>Cancel</Button>
            <Button
              variant="destructive"
              onClick={async () => {
                if (!removing) return;
                try {
                  const result = await api.removeDocument(removing.source);
                  toast.push({ title: `Removed ${result.source}`, description: `${result.removed_chunks} chunks dropped${result.file_deleted ? ", file deleted" : ""}. Index now ${result.index_size} chunks.`, variant: "success" });
                  setRemoving(null);
                  void overview.refresh();
                } catch (err) {
                  toast.push({ title: "Could not remove the document", description: errorMessage(err), variant: "error" });
                }
              }}
            >
              <Trash2 className="size-4" /> Remove
            </Button>
          </>
        }
      >
        {removing ? (
          <p className="text-sm text-muted-foreground">
            <span className="font-mono text-foreground">{removing.source}</span>: its {removing.chunks} chunks leave the index and the file is deleted from the documents folder. The delegate can no longer answer from it.
          </p>
        ) : null}
      </Dialog>
    </>
  );
}

function HealthCard({ data }: { data: KnowledgeOverview | undefined }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Database className="size-4 text-muted-foreground" /> ChromaDB collection
        </CardTitle>
        <CardDescription>Local vector index, embedded on the CPU.</CardDescription>
      </CardHeader>
      <CardContent>
        {!data ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              {data.available ? <Badge variant="success" dot>Healthy</Badge> : <Badge variant="danger" dot>Unavailable</Badge>}
              <span className="tabular text-2xl font-semibold tracking-tight">{data.index_size}</span>
              <span className="text-xs text-muted-foreground">chunks · {data.documents.length} document{data.documents.length === 1 ? "" : "s"}</span>
            </div>
            {data.detail ? <p className="text-xs text-danger">{data.detail}</p> : null}
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
              <Row k="Collection" v={data.collection} mono />
              <Row k="Model" v={data.embedding_model} mono />
              <Row k="Threshold" v={`cosine ≥ ${data.min_similarity.toFixed(2)}`} />
              <Row k="Retrieval" v={`top ${data.top_k}`} />
              <Row k="Chunking" v={`${data.chunk_size} chars, ${data.chunk_overlap} overlap`} />
              <Row k="Index dir" v={data.chroma_dir} mono />
              <Row k="Documents" v={data.documents_dir} mono />
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Row({ k, v, mono = false }: { k: string; v: string; mono?: boolean }) {
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className={cn("min-w-0 truncate", mono && "font-mono text-[11px]")} title={v}>{v}</dd>
    </>
  );
}

function UploadCard({ data, onDone }: { data: KnowledgeOverview | undefined; onDone: () => void }) {
  const [queue, setQueue] = React.useState<File[]>([]);
  const [dragging, setDragging] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [results, setResults] = React.useState<FileIngestResult[] | null>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const toast = useToast();
  const accept = (data?.supported_suffixes ?? [".md", ".markdown", ".txt", ".pdf"]).join(",");
  const limit = data?.upload_limit_bytes ?? 25 * 1024 * 1024;

  function addFiles(list: FileList | File[]) {
    const incoming = Array.from(list);
    setQueue((current) => {
      const names = new Set(current.map((f) => f.name));
      return [...current, ...incoming.filter((f) => !names.has(f.name))];
    });
    setResults(null);
  }

  async function upload() {
    if (!queue.length) return;
    setBusy(true);
    try {
      const report = await api.upload(queue);
      setResults(report.files);
      const ok = report.files.filter((f) => f.status === "ingested").length;
      const failed = report.files.filter((f) => f.status === "failed").length;
      toast.push({
        title: `${ok} document${ok === 1 ? "" : "s"} indexed${failed ? `, ${failed} failed` : ""}`,
        description: `Index now holds ${report.index_size} chunks.`,
        variant: failed && !ok ? "error" : "success",
      });
      setQueue([]);
      onDone();
    } catch (err) {
      toast.push({ title: "Upload failed", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="h-full">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Upload className="size-4 text-muted-foreground" /> Add documents
        </CardTitle>
        <CardDescription>Markdown, plain text, or PDF. Files are saved to the documents folder and embedded immediately. Re-uploading a changed file replaces its chunks.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer.files);
          }}
          className={cn(
            "flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-md border border-dashed p-6 text-center transition-colors",
            dragging ? "border-info bg-info/5" : "border-border hover:bg-muted/50",
            !data?.available && "pointer-events-none opacity-60",
          )}
          aria-disabled={!data?.available}
        >
          <FileText className="size-5 text-muted-foreground" />
          <p className="text-sm font-medium">Drop files here or click to browse</p>
          <p className="text-xs text-muted-foreground">{accept.replace(/,/g, " · ")} · up to {formatBytes(limit)} each</p>
          <input ref={inputRef} type="file" multiple accept={accept} className="hidden" onChange={(e) => e.target.files && addFiles(e.target.files)} />
        </div>

        {queue.length ? (
          <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
            {queue.map((file) => {
              const tooBig = file.size > limit;
              return (
                <li key={file.name} className="flex items-center gap-3 px-3 py-2 text-xs">
                  <FileText className="size-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate font-mono">{file.name}</span>
                  <span className={cn("tabular text-muted-foreground", tooBig && "text-danger")}>{formatBytes(file.size)}</span>
                  <button type="button" className="text-muted-foreground hover:text-foreground" onClick={() => setQueue((q) => q.filter((f) => f !== file))} aria-label={`Remove ${file.name}`}>
                    ×
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}

        {results ? (
          <ul className="flex flex-col gap-1 text-xs">
            {results.map((r) => (
              <li key={r.source} className="flex flex-wrap items-center gap-2">
                <Badge variant={r.status === "ingested" ? "success" : r.status === "skipped" ? "outline" : "danger"}>{r.status}</Badge>
                <span className="font-mono">{r.source}</span>
                <span className="text-muted-foreground">{r.status === "failed" ? r.detail : `${r.chunks} chunks · ${r.characters.toLocaleString()} chars${r.status === "skipped" ? " · unchanged" : ""}`}</span>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">{queue.length ? `${queue.length} file${queue.length === 1 ? "" : "s"} ready` : "Nothing queued"}</span>
          <Button onClick={upload} disabled={!queue.length || queue.some((f) => f.size > limit) || !data?.available} loading={busy}>
            <Upload className="size-4" /> Upload and index
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function DocumentsCard({ data, onChunks, onRemove, onChanged }: { data: KnowledgeOverview | undefined; onChunks: (doc: IndexedDocument) => void; onRemove: (doc: IndexedDocument) => void; onChanged: () => void }) {
  const [force, setForce] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const toast = useToast();

  async function reindex() {
    setBusy(true);
    try {
      const report = await api.reindex(force);
      const ingested = report.files.filter((f) => f.status === "ingested").length;
      const skipped = report.files.filter((f) => f.status === "skipped").length;
      toast.push({ title: "Folder re-indexed", description: `${ingested} ingested, ${skipped} unchanged, ${report.index_size} chunks in ${report.elapsed_seconds.toFixed(1)} s.`, variant: "success" });
      onChanged();
    } catch (err) {
      toast.push({ title: "Re-index failed", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 pb-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Layers className="size-4 text-muted-foreground" /> Indexed documents
          </CardTitle>
          <CardDescription>Each source with the chunks it contributes. Open a row to read exactly what the delegate can quote.</CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <div className="flex items-center gap-2">
            <Switch id="force" checked={force} onCheckedChange={setForce} aria-label="Re-embed unchanged files" />
            <Label htmlFor="force" className="font-normal text-muted-foreground">force</Label>
          </div>
          <Button variant="outline" size="sm" onClick={reindex} loading={busy} disabled={!data?.available} title="Re-scan the documents folder">
            <FolderSync className="size-3.5" /> Re-index folder
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {!data ? (
          <Skeleton className="h-32" />
        ) : data.documents.length === 0 && data.unindexed_files.length === 0 ? (
          <EmptyState compact icon={<Layers />} title="The index is empty" description="Upload a document above, or drop files into the documents folder and re-index." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="pb-2 pr-3 font-medium">Source</th>
                  <th className="pb-2 pr-3 font-medium">Title</th>
                  <th className="pb-2 pr-3 text-right font-medium">Chunks</th>
                  <th className="pb-2 pr-3 text-right font-medium">Size</th>
                  <th className="pb-2 pr-3 font-medium">Updated</th>
                  <th className="pb-2 font-medium sr-only">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.documents.map((doc) => (
                  <tr key={doc.source} className="group">
                    <td className="py-2 pr-3">
                      <button type="button" onClick={() => onChunks(doc)} className="font-mono text-xs underline-offset-2 hover:underline">{doc.source}</button>
                      {!doc.on_disk ? (
                        <span className="ml-2 inline-flex items-center gap-1 text-[11px] text-warning-foreground" title="Indexed but the file is no longer in the documents folder">
                          <AlertTriangle className="size-3" /> file missing
                        </span>
                      ) : null}
                    </td>
                    <td className="max-w-[16rem] truncate py-2 pr-3 text-xs" title={doc.title}>{doc.title || "—"}</td>
                    <td className="tabular py-2 pr-3 text-right text-xs">{doc.chunks}{doc.pages ? <span className="text-muted-foreground"> · {doc.pages} p</span> : null}</td>
                    <td className="tabular py-2 pr-3 text-right text-xs text-muted-foreground">{formatBytes(doc.size_bytes)}</td>
                    <td className="py-2 pr-3 text-xs text-muted-foreground">{doc.modified_at ? formatDateTime(doc.modified_at) : "—"}</td>
                    <td className="py-2 text-right">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="sm" onClick={() => onChunks(doc)}>Chunks</Button>
                        <Button variant="ghost" size="sm" className="hover:text-danger" onClick={() => onRemove(doc)} aria-label={`Remove ${doc.source}`}>
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {data.unindexed_files.map((file) => (
                  <tr key={`u-${file.source}`} className="text-muted-foreground">
                    <td className="py-2 pr-3 font-mono text-xs">{file.source}</td>
                    <td className="py-2 pr-3 text-xs">
                      <Badge variant="warning">not indexed</Badge>
                    </td>
                    <td className="py-2 pr-3 text-right text-xs">—</td>
                    <td className="tabular py-2 pr-3 text-right text-xs">{formatBytes(file.size_bytes)}</td>
                    <td className="py-2 pr-3 text-xs">{formatDateTime(file.modified_at)}</td>
                    <td className="py-2 text-right text-xs">re-index to add</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ChunksDialog({ document: doc, onClose }: { document: IndexedDocument | null; onClose: () => void }) {
  const page = useApi<ChunkPage>(() => api.knowledgeChunks(doc?.source ?? "", 500), { enabled: Boolean(doc), key: doc?.source ?? "" });
  return (
    <Dialog open={Boolean(doc)} onClose={onClose} size="xl" title={doc ? doc.title || doc.source : ""} description={doc ? `${doc.source} · ${doc.chunks} chunks · sha256 ${doc.sha256?.slice(0, 12) ?? "?"}` : undefined}>
      {page.loading && !page.data ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      ) : page.error ? (
        <p className="text-sm text-danger">{page.error}</p>
      ) : (
        <ol className="flex flex-col gap-3">
          {page.data?.chunks.map((chunk) => {
            const [section, ...body] = chunk.text.split("\n");
            return (
              <li key={chunk.chunk_id} className="rounded-md border border-border">
                <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-3 py-1.5 text-[11px]">
                  <span className="tabular font-mono text-muted-foreground">#{chunk.chunk_index}</span>
                  <span className="font-medium">{chunk.section || section}</span>
                  {chunk.page !== null ? <span className="text-muted-foreground">p. {chunk.page}</span> : null}
                  <span className="ml-auto tabular text-muted-foreground">{chunk.characters} chars</span>
                </div>
                <pre className="scroll-thin max-h-64 overflow-auto whitespace-pre-wrap px-3 py-2 font-mono text-[12px] leading-relaxed text-foreground/90">{body.length ? body.join("\n") : chunk.text}</pre>
              </li>
            );
          })}
        </ol>
      )}
    </Dialog>
  );
}

function RetrievalTester({ data }: { data: KnowledgeOverview | undefined }) {
  const [query, setQuery] = React.useState("");
  const [result, setResult] = React.useState<RetrievalResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.query(query.trim()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const threshold = result?.threshold ?? data?.min_similarity ?? 0.3;
  const best = result?.best_similarity ?? null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Search className="size-4 text-muted-foreground" /> Test retrieval
        </CardTitle>
        <CardDescription>Ask what a participant might ask and see whether the similarity gate would let the delegate answer.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form onSubmit={run} className="flex gap-2">
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="What database is Project Alpha using?" disabled={!data?.available} />
          <Button type="submit" loading={busy} disabled={!query.trim() || !data?.available}>Search</Button>
        </form>
        {error ? <p className="text-xs text-danger">{error}</p> : null}
        {result ? (
          <div className="flex flex-col gap-3 fade-in">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant={result.grounded ? "success" : "outline"}>{result.status.replace("_", " ")}</Badge>
              <span className="text-muted-foreground">{result.chunks.length} of top {data?.top_k ?? "?"} cleared · {result.elapsed_ms.toFixed(0)} ms</span>
            </div>
            <div>
              <div className="mb-1 flex justify-between text-[11px] text-muted-foreground">
                <span>best similarity {best !== null ? best.toFixed(3) : "—"}</span>
                <span>threshold {threshold.toFixed(2)}</span>
              </div>
              <div className="relative h-2 overflow-hidden rounded-full bg-muted">
                <div className={cn("h-full rounded-full transition-all", result.grounded ? "bg-success" : "bg-warning")} style={{ width: `${Math.max(0, Math.min(1, best ?? 0)) * 100}%` }} />
                <div className="absolute top-0 h-full w-px bg-foreground/60" style={{ left: `${threshold * 100}%` }} aria-hidden="true" />
              </div>
            </div>
            {result.chunks.length ? (
              <ul className="flex flex-col gap-2">
                {result.chunks.map((chunk) => (
                  <li key={chunk.chunk_id} className="rounded-md border border-border p-2.5 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium" title={chunk.section}>{chunk.section || chunk.title}</span>
                      <span className="tabular shrink-0 text-muted-foreground">{chunk.similarity.toFixed(3)}</span>
                    </div>
                    <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-muted-foreground">{chunk.text.split("\n").slice(1).join("\n") || chunk.text}</p>
                    <p className="mt-1 font-mono text-[10px] text-muted-foreground">{chunk.source}{chunk.page !== null ? ` · p. ${chunk.page}` : ""}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">Nothing cleared the gate, so the delegate would treat this as unknown context and escalate or stay silent rather than guess.</p>
            )}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
