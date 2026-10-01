"""Milvus vector store — the dense leg's storage backend.

The retrieval chain treats "give me a query vector, return ranked chunk
ids" as a narrow interface, which is what let the first iteration run on a
plain JSONL cache with brute-force cosine (2125 chunks: exact search, no
service, no dependency). This module is the second implementation of that
interface: Milvus. Same contract, so the chain above it — fusion, rerank,
rewrite, evaluation — does not change at all.

Design decisions worth remembering:

- FLAT index, not HNSW. At this corpus size (a couple thousand chunks) the
  exact search costs milliseconds and the evaluation numbers must match the
  brute-force baseline *exactly* — an approximate index would make the
  55-question regression drift for no benefit. Switch to HNSW when the
  corpus (or the latency budget) actually demands it.
- Embeddings are still produced by the bge-m3 API outside Milvus. Milvus
  stores and searches vectors; it does not vectorize. The content-hash
  cache key from the JSONL era survives as a scalar field, so switching
  embedding models re-embeds everything instead of mixing vector spaces.
- `sync_index` is idempotent: it upserts only chunks whose content hash is
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


@dataclass
class MilvusVectorStore:
    """Dense-leg storage on Milvus. One collection, FLAT index, COSINE."""

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

    def ensure_collection(self, client: Any) -> None:
        """Create the collection with an explicit schema and FLAT index.

        The explicit schema (instead of MilvusClient's quick-create) pins
        the scalar fields the chain needs back: chapter_id and heading for
        result display, content_hash for idempotent syncs.
        """
        from pymilvus import DataType

        _uri, collection = self.configuration()
        if client.has_collection(collection):
            return
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=256)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field("chapter_id", DataType.VARCHAR, max_length=128)
        schema.add_field("heading", DataType.VARCHAR, max_length=512)
        schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
        index_params = client.prepare_index_params()
        # FLAT = exact search. Matches the brute-force baseline bit for bit;
        # see module docstring before switching to an ANN index.
        index_params.add_index(
            field_name="vector", index_type="FLAT", metric_type="COSINE"
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
        _uri, collection = self.configuration()
        if not client.has_collection(collection):
            return []
        search_filter = None
        if chapter_filter:
            search_filter = f'chapter_id == "{chapter_filter}"'
        results = client.search(
            collection,
            data=[query_vector],
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
