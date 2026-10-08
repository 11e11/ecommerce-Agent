"""Milvus vector store — the hybrid retrieval backend (BM25 + dense).

The retrieval chain treats "give me a query, return ranked chunk ids" as a
narrow interface, which is what let the first iteration run on a plain
JSONL cache with brute-force cosine plus an in-process BM25 (2125 chunks:
exact search, no service, no dependency). This module is the production
implementation of that interface: Milvus 2.5+ runs *both* legs in-database —
dense vector search over the stored embeddings, and BM25 full-text search
via a BM25 Function over an analyzer-enabled text field — and fuses the two
rankings with RRFRanker inside the same call. The chain above it (rewrite,
rerank, grading, corrective second pass, evaluation) does not change.

Design decisions worth remembering:

- FLAT index, not HNSW. At this corpus size (a couple thousand chunks) the
  exact search costs milliseconds and the evaluation numbers must match the
  brute-force baseline *exactly* — an approximate index would make the
  55-question regression drift for no benefit. Switch to HNSW when the
  corpus (or the latency budget) actually demands it.
- The keyword leg lives in Milvus as a BM25 Function over a text field with
  the built-in ``chinese`` analyzer (jieba tokenizer). Analyzer choice is
  server-side and shared by indexing and querying, so index/query
  tokenization can never drift. The in-process BM25 (retrieval.py) remains
  as the zero-dependency fallback path when no Milvus is configured.
- RRFRanker(60) mirrors the application-layer RRF_K, so both backends vote
  with the same formula — the backend switch changes *where* fusion runs,
  not *how*.
- Embeddings are still produced by the bge-m3 API outside Milvus. Milvus
  stores and searches vectors; it does not vectorize. The content-hash
  cache key from the JSONL era survives as a scalar field, so switching
  embedding models re-embeds everything instead of mixing vector spaces.
- Schema migration: a collection created before hybrid search (no sparse
  field) cannot gain one in place, so ``ensure_collection`` drops and
  recreates it. Repopulation is cheap and idempotent — every embedding is
  already in the content-hash cache, so ``rag-index`` re-upserts all rows
  without re-paying the embedding API.
- `build` is idempotent: it upserts only chunks whose content hash is
  missing or changed. Rebuilding the index after editing one chapter does
  not re-embed or re-write the other two thousand.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConnectorNotConfiguredError, ValidationError
from .retrieval import (
    DEFAULT_CACHE_DIR,
    ENV_RAG_CACHE,
    RRF_K,
    EmbeddingCache,
    EmbeddingClient,
    chunk_corpus,
)

log = logging.getLogger(__name__)

USER_AGENT = "opc-ecommerce-rag/1.0"

ENV_MILVUS_URI = "MILVUS_URI"
ENV_MILVUS_COLLECTION = "MILVUS_COLLECTION"

DEFAULT_MILVUS_URI = "http://127.0.0.1:19530"
DEFAULT_COLLECTION = "opc_knowledge"

# Server-side analyzer for the BM25 leg. "chinese" is Milvus's jieba-based
# language analyzer — it segments Chinese prose into words and keeps ASCII
# runs (platform names, "IPI", "FBA") as single tokens, both when indexing
# and when parsing the query text, so the two sides can never disagree.
ANALYZER_PARAMS = {"type": "chinese"}
BM25_FUNCTION_NAME = "text_bm25"


@dataclass
class MilvusVectorStore:
    """Hybrid (BM25 + dense) storage on Milvus, fused by RRFRanker in-DB."""

    uri: str | None = None
    collection: str | None = None
    dimension: int = 1024
    timeout_seconds: int = 30
    environ: Mapping[str, str] | None = None
    def _environment(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def configuration(self) -> tuple[str, str]:
        env = self._environment()
        uri = self.uri or env.get(ENV_MILVUS_URI, "").strip() or DEFAULT_MILVUS_URI
        collection = (
            self.collection
            or env.get(ENV_MILVUS_COLLECTION, "").strip()
            or DEFAULT_COLLECTION
        )
        if not uri.startswith(("http://", "https://")):
            raise ValidationError("milvus uri must be an http(s) URL")
        return uri.rstrip("/"), collection

    def client(self) -> Any:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise ConnectorNotConfiguredError(
                "pymilvus is not installed; install with: pip install 'pymilvus>=2.5,<3'"
            ) from exc
        uri, _collection = self.configuration()
        return MilvusClient(uri=uri, timeout=self.timeout_seconds)

    def _collection_fields(self, client: Any, collection: str) -> set[str]:
        try:
            description = client.describe_collection(collection)
        except Exception:
            return set()
        return {
            field.get("name")
            for field in (description.get("fields") or [])
            if isinstance(field, dict) and field.get("name")
        }

    def supports_hybrid(self, client: Any, collection: str | None = None) -> bool:
        """True when the collection has the BM25 sparse field, i.e. the
        in-database hybrid search can run against it."""
        _uri, name = self.configuration()
        fields = self._collection_fields(client, collection or name)
        return "sparse" in fields and "text" in fields

    def ensure_collection(self, client: Any) -> None:
        """Create the hybrid schema, migrating a legacy collection if needed.

        The explicit schema (instead of MilvusClient's quick-create) pins the
        fields the chain needs back: chapter_id and heading for result
        display, content_hash for idempotent syncs, and the BM25 pair — an
        analyzer-enabled text field feeding a sparse vector field through a
        BM25 Function. A pre-hybrid collection has no sparse field and
        functions cannot be attached in place, so it is dropped and recreated;
        the index builder refills it from the embedding cache with no API cost.
        """
        from pymilvus import DataType, Function, FunctionType

        _uri, collection = self.configuration()
        if client.has_collection(collection):
            if self.supports_hybrid(client, collection):
                return
            log.warning(
                "collection %s uses the pre-hybrid schema (no BM25 sparse "
                "field); dropping and recreating — re-run rag-index to "
                "repopulate it from the embedding cache",
                collection,
            )
            client.drop_collection(collection)
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=256)
        schema.add_field(
            "text", DataType.VARCHAR, max_length=65535,
            enable_analyzer=True, analyzer_params=ANALYZER_PARAMS,
            enable_match=True,
        )
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("chapter_id", DataType.VARCHAR, max_length=128)
        schema.add_field("heading", DataType.VARCHAR, max_length=512)
        schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
        schema.add_function(Function(
            name=BM25_FUNCTION_NAME,
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse"],
        ))
        index_params = client.prepare_index_params()
        # FLAT = exact search. Matches the brute-force baseline bit for bit;
        # see module docstring before switching to an ANN index.
        index_params.add_index(
            field_name="vector", index_type="FLAT", metric_type="COSINE"
        )
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        client.create_collection(
            collection, schema=schema, index_params=index_params
        )

    def upsert(self, client: Any, rows: list[dict[str, Any]]) -> int:
        _uri, collection = self.configuration()
        written = 0
        batch_size = 500
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            result = client.upsert(collection, data=batch)
            written += len(batch)
            log.debug("milvus upsert batch %s: %s", start, result)
        return written

    def existing_hashes(self, client: Any) -> dict[str, str]:
        """chunk_id -> content_hash for everything already indexed."""
        _uri, collection = self.configuration()
        if not client.has_collection(collection):
            return {}
        rows = client.query(
            collection,
            filter="chunk_id != ''",
            output_fields=["chunk_id", "content_hash"],
            limit=16384,
        )
        return {
            row["chunk_id"]: row.get("content_hash", "")
            for row in rows or []
            if isinstance(row, dict) and row.get("chunk_id")
        }

    def search(
        self,
        client: Any,
        query_vector: list[float],
        top_k: int,
        chapter_filter: str | None = None,
    ) -> list[tuple[str, float]]:
        """Dense-only search. Kept for the legacy-schema path and direct
        diagnostics; the chain prefers ``hybrid_search`` when the collection
        carries the BM25 sparse field."""
        _uri, collection = self.configuration()
        if not client.has_collection(collection):
            return []
        search_filter = None
        if chapter_filter:
            search_filter = f'chapter_id == "{chapter_filter}"'
        results = client.search(
            collection,
            data=[query_vector],
            anns_field="vector",
            limit=top_k,
            filter=search_filter,
            output_fields=["chunk_id"],
        )
        hits = results[0] if results else []
        return [
            (hit["id"], float(hit["distance"]))
            for hit in hits or []
            if isinstance(hit, dict) and hit.get("id")
        ]

    def hybrid_search(
        self,
        client: Any,
        query_text: str,
        query_vector: list[float],
        top_k: int,
        chapter_filter: str | None = None,
    ) -> list[tuple[str, float]]:
        """One fused ranking from both legs, computed inside Milvus.

        Dense leg: the stored embedding vs the query vector (COSINE, FLAT).
        Keyword leg: the raw query text, tokenized server-side by the same
        ``chinese`` analyzer that indexed the corpus, scored by the BM25
        Function into the sparse field. RRFRanker(60) votes the two lists
        together with the same formula the application-layer fallback uses,
        so switching backends changes where fusion runs, not how.
        """
        from pymilvus import AnnSearchRequest, RRFRanker

        _uri, collection = self.configuration()
        if not client.has_collection(collection):
            return []
        search_filter = None
        if chapter_filter:
            search_filter = f'chapter_id == "{chapter_filter}"'
        dense_request = AnnSearchRequest(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "COSINE"},
            limit=top_k,
            expr=search_filter,
        )
        # drop_ratio_search=0: never trade recall for speed on a corpus this
        # small — the sparse leg must stay deterministic for the eval.
        sparse_request = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse",
            param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.0}},
            limit=top_k,
            expr=search_filter,
        )
        results = client.hybrid_search(
            collection,
            reqs=[dense_request, sparse_request],
            ranker=RRFRanker(RRF_K),
            limit=top_k,
            output_fields=["chunk_id"],
        )
        hits = results[0] if results else []
        parsed: list[tuple[str, float]] = []
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            # hybrid_search result dicts key the primary key under
            # "chunk_id" (the output field), not "id" like client.search.
            chunk_id = (
                hit.get("id")
                or hit.get("chunk_id")
                or (hit.get("entity") or {}).get("chunk_id")
            )
            if chunk_id:
                parsed.append((str(chunk_id), float(hit.get("distance", 0.0))))
        return parsed


@dataclass
class IndexBuilder:
    """Chunk the corpus, reuse cached embeddings, sync into Milvus."""

    dist_path: str | Path
    store: MilvusVectorStore
    embed_client: EmbeddingClient
    cache_path: str | Path | None = None
    environ: Mapping[str, str] | None = None

    def build(self) -> dict[str, Any]:
        client = self.store.client()
        self.store.ensure_collection(client)
        _uri, model = self.embed_client.configuration()
        cache = EmbeddingCache(self._cache_path())
        chunks = chunk_corpus(self.dist_path)
        existing = self.store.existing_hashes(client)

        rows: list[dict[str, Any]] = []
        missing_vectors: list[Chunk] = []
        for chunk in chunks:
            digest = EmbeddingCache.key(model, chunk.text)
            if existing.get(chunk.chunk_id) == digest:
                continue
            vector = cache.get(model, chunk.text)
            if vector is None:
                missing_vectors.append(chunk)
                continue
            rows.append(self._row(chunk, vector, digest))

        if missing_vectors:
            vectors = self.embed_client.embed(
                [chunk.text for chunk in missing_vectors]
            )
            for chunk, vector in zip(missing_vectors, vectors):
                cache.put(model, chunk.text, vector)
                rows.append(self._row(chunk, vector, EmbeddingCache.key(model, chunk.text)))

        written = self.store.upsert(client, rows) if rows else 0
        return {
            "collection": self.store.configuration()[1],
            "chunk_count": len(chunks),
            "upserted": written,
            "unchanged": len(chunks) - len(rows),
        }

    def _row(self, chunk: Chunk, vector: list[float], digest: str) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "vector": vector,
            "chapter_id": chunk.chapter_id,
            "heading": chunk.heading,
            "content_hash": digest,
        }

    def _cache_path(self) -> Path:
        if self.cache_path is not None:
            return Path(self.cache_path)
        env = self.environ if self.environ is not None else os.environ
        return Path(env.get(ENV_RAG_CACHE, "") or DEFAULT_CACHE_DIR) / "embeddings.jsonl"
