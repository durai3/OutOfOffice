"""
Offline check of the retriever on the bundled sample documents (real embeddings, a throwaway index).

Dense embeddings alone blur short factual chunks behind longer prose that merely
mentions the same product. Two things in :mod:`src.knowledge.retriever` correct
for that, and this pins both:

* a **lexical reranker** over an over-fetched candidate pool, so the chunk that
  literally contains the word asked about is not dropped by top-k;
* **clause-aware retrieval**, so a two-part question retrieves for both parts.

It also pins what must NOT change: an unrelated question still retrieves nothing.

Usage::

    python tests/test_hybrid_retrieval.py

Exit codes: 0 = every check passed, 1 = at least one failed. No network, no API key.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.config import KnowledgeSettings  # noqa: E402
from src.knowledge.ingest import DocumentIngestor  # noqa: E402
from src.knowledge.retriever import KnowledgeRetriever, split_compound_query  # noqa: E402
from src.knowledge.store import KnowledgeStore  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [✓] {label}")
    else:
        _FAILED += 1
        print(f"  [✗] {label}{f' - {detail}' if detail else ''}")


def check_splitter() -> None:
    print("\n1. split_compound_query()")
    check("a single question is left whole", split_compound_query("What is the status of Project Atlas?") == [])
    parts = split_compound_query("What database is Atlas running on, and what is the median dashboard load time?")
    check("a two-part question becomes two clauses", len(parts) == 2, str(parts))
    check("each clause keeps its own subject", "database" in parts[0] and "load time" in parts[1], str(parts))
    check("a bare connective is not a clause", split_compound_query("Tell me more and what") == [])
    many = split_compound_query("budget, team, roadmap, risks, metrics, beta, launch, staffing, pricing")
    check("the clause count is capped", len(many) <= 3, str(many))


async def check_retrieval(root: Path) -> None:
    print("\n2. retrieval over the sample documents")
    docs = root / "documents"
    docs.mkdir()
    for source in sorted((PROJECT_ROOT / "demo" / "documents").glob("*.md")):
        (docs / source.name).write_text(source.read_text().replace("{{name}}", "Alex Morgan"))
    settings = KnowledgeSettings.for_tenant(docs, "test_hybrid_retrieval", root / "chroma")
    store = KnowledgeStore(settings)
    await store.open_async()
    try:
        report = await DocumentIngestor(store).ingest_directory(docs)
        check("three documents indexed", len([f for f in report.files if f.status != "failed"]) == 3)
        retriever = KnowledgeRetriever(store)

        async def top(query: str) -> list[str]:
            result = await retriever.retrieve(query, min_similarity=0.22)
            return [chunk.text for chunk in result.chunks]

        db = await top("What database does Atlas use?")
        check("the chunk that names the database is retrieved", any("PostgreSQL 16" in t for t in db), f"{len(db)} chunks")

        both = await top("What database is Atlas running on, and what is the median dashboard load time?")
        check("a compound question retrieves the database half", any("PostgreSQL 16" in t for t in both))
        check("a compound question retrieves the load-time half", any("1.4 seconds" in t for t in both))

        lead = await top("Who is the engineering lead on Atlas?")
        check("a named-role question finds the person", any("Priya Raman" in t for t in lead))

        for unrelated in ("What is the weather in Paris today?", "How do I bake sourdough bread?"):
            check(f"unrelated question retrieves nothing: {unrelated!r}", await top(unrelated) == [])

        result = await retriever.retrieve("What database does Atlas use?", min_similarity=0.22)
        check("never returns more than top_k plus the compound allowance", len(result.chunks) <= settings.top_k + 2)
        check("scores stay within [0, 1]", all(0.0 <= c.similarity <= 1.0 for c in result.chunks))
    finally:
        store.close()


def main() -> int:
    print("DELEG8 hybrid retrieval: offline verification")
    check_splitter()
    with tempfile.TemporaryDirectory(prefix="deleg8-retrieval-") as tmp:
        asyncio.run(check_retrieval(Path(tmp)))
    print(f"\n{_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
