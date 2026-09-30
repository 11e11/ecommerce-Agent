"""Hybrid retrieval (RAG) over the packaged knowledge corpus.

The knowledge MCP tools answer "where is this written down" with substring
matching only: no tokenizer, no ranking, no second look. That is enough to
route a human, not enough to ground an agent answer that cites the right
paragraph. This module adds the retrieval chain the research phase was
missing (architecture after NirDiamant/RAG_Techniques, metric conventions
after RUC-NLPIR/FlashRAG and vibrantlabsai/ragas):

    chunk -> [query rewrite] -> BM25 + dense vectors -> RRF fusion
          -> [listwise rerank] -> [relevance grading]
          -> corrective second pass when evidence is thin
          -> post-generation faithfulness check

Design constraints inherited from the runtime:

- stdlib only. HTTP goes through an injectable ``transport`` callable the
  same way the agent providers do, so no SDK, no httpx, and tests can deny
  the network entirely.
- Every LLM step is optional and fails soft: a provider error (missing
  credential, no balance, HTTP failure) degrades that one step to a warning
  and the chain still returns fused retrieval results.
- Dense vectors come from an OpenAI-compatible /embeddings endpoint
  (SiliconFlow by default) and are content-hashed into an on-disk cache, so
  the full-corpus embedding pass happens exactly once per model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    ConnectorNotConfiguredError,
    ExternalServiceError,
    MissingCredentialError,
    ValidationError,
)

log = logging.getLogger(__name__)

USER_AGENT = "opc-ecommerce-rag/1.0"

ENV_EMBEDDING_BASE_URL = "EAI_EMBEDDING_BASE_URL"
ENV_EMBEDDING_API_KEY = "EAI_EMBEDDING_API_KEY"
ENV_EMBEDDING_MODEL = "EAI_EMBEDDING_MODEL"
ENV_RERANK_MODEL = "EAI_RERANK_MODEL"
ENV_RERANK_API_KEY = "EAI_RERANK_API_KEY"
ENV_RAG_CACHE = "EAI_RAG_CACHE"

DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "opc-rag"

RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150
CHUNK_MIN_CHARS = 80
EMBED_BATCH_SIZE = 16
RERANK_POOL = 10
GRADE_POOL = 8
MIN_RELEVANT_CHUNKS = 2
EXCERPT_CHARS = 400

REWRITE_AGENT = "rag_query_rewriter"
RERANK_AGENT = "rag_listwise_reranker"
GRADE_AGENT = "rag_relevance_grader"
GENERATE_AGENT = "rag_answer_generator"
FAITHFULNESS_AGENT = "rag_faithfulness_checker"

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "rewritten_query": {"type": "string"},
        "alternate_query": {"type": "string"},
    },
    "required": ["rewritten_query", "alternate_query"],
    "additionalProperties": False,
}
RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["ranking"],
    "additionalProperties": False,
}
GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant_indices": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["relevant_indices"],
    "additionalProperties": False,
}
GENERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer"],
    "additionalProperties": False,
}
FAITHFULNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                },
                "required": ["claim", "supported"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}

_ASCII_WORD = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def tokenize(text: str) -> list[str]:
    """ASCII words plus CJK character bigrams.

    Chinese has no whitespace to split on, and the corpus is Chinese prose
    studded with English terms; bigrams give BM25 something to match that
    survives inflection and spacing noise.
    """
    lowered = text.lower()
    tokens = _ASCII_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a bare KEY=VALUE file. Existing process env always wins."""
    entries: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.is_file():
        return entries
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not value:
            continue
        entries[key] = value
    return entries


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    chapter_id: str
    chapter_title: str
    heading: str
    position: int
    text: str


def chunk_corpus(dist_path: str | Path) -> list[Chunk]:
    """Split every packaged chapter into heading-sized chunks.

    ``## `` headings are the corpus's own unit of thought — prompt templates,
    checklists, and metric tables each live under one — so they make natural
    retrieval units. Oversized sections are hard-split into overlapping
    windows so no fact gets cut by a window boundary twice.
    """
    dist = Path(dist_path)
    index = json.loads(
        (dist / "knowledge" / "index.json").read_text(encoding="utf-8")
    )
    chunks: list[Chunk] = []
    for entry in sorted(index, key=lambda item: item["id"]):
        chapter_id = entry["id"]
        body_path = dist / "knowledge" / entry["body_path"]
        text = body_path.read_text(encoding="utf-8")
        first_line = text.splitlines()[0].lstrip("# ").strip() if text else ""
        sections = _merged_sections(_split_sections(text))
        position = 0
        for heading, body in sections:
            for window in _windows(body, CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS):
                if len(window.strip()) < CHUNK_MIN_CHARS:
                    continue
                chunks.append(Chunk(
                    chunk_id=f"{chapter_id}#{position}",
                    chapter_id=chapter_id,
                    chapter_title=first_line,
                    heading=heading,
                    position=position,
                    text=window.strip(),
                ))
                position += 1
    chunks.extend(_constraint_chunks(dist))
    return chunks


def _merged_sections(
    sections: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Fold undersized sections into their predecessor.

    A five-line section (a formula, a one-rule checklist) makes a useless
    chunk on its own and would be dropped by the min-size filter, silently
    losing its facts from the index. Merged forward, the text stays
    retrievable; the heading is kept as ``prev / tiny`` so heading anchors
    still resolve.
    """
    merged: list[list[str]] = []
    for heading, body in sections:
        if merged and len(body.strip()) < CHUNK_MIN_CHARS:
            merged[-1][0] = f"{merged[-1][0]} / {heading}"
            merged[-1][1] = f"{merged[-1][1]}\n\n{heading}\n\n{body}"
        else:
            merged.append([heading, body])
    return [(heading, body) for heading, body in merged]


def _split_sections(text: str) -> list[tuple[str, str]]:
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [("(overview)", text)]
    sections: list[tuple[str, str]] = []
    preamble = text[:matches[0].start()]
    if preamble.strip():
        sections.append(("(overview)", preamble))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = match.group(1).strip()
        sections.append((heading, text[match.end():end]))
    return sections


def _windows(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    step = size - overlap
    return [text[start:start + size] for start in range(0, len(text), step)]


def _constraint_chunks(dist_path: Path) -> list[Chunk]:
    """Package ontology constraints as first-class retrieval chunks.

    Constraints are short, dense, sourced statements (each carries a source
    anchor into a chapter) — exactly the facts a compliance or inventory
    question hinges on, and cheap insurance when chapter prose buries them.
    """
    ontology_path = Path(dist_path) / "ontology.json"
    if not ontology_path.is_file():
        return []
    try:
        ontology = json.loads(ontology_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    chunks: list[Chunk] = []
    for position, constraint in enumerate(ontology.get("constraints", [])):
        statement = constraint.get("statement", {}) or {}
        text = " ".join(
            part for part in (
                str(constraint.get("id", "")),
                str(constraint.get("attribute", "")),
                str(constraint.get("value", "")),
                str(constraint.get("unit", "")),
                str(statement.get("zh", "")),
                str(statement.get("en", "")),
            ) if part
        )
        chunks.append(Chunk(
            chunk_id=f"constraint__{constraint.get('id', position)}",
            chapter_id="ontology__constraints",
            chapter_title="平台约束清单 | Platform Constraints",
            heading=str(constraint.get("id", f"constraint-{position}")),
            position=position,
            text=text,
        ))
    return chunks


class BM25Index:
    """In-repo BM25 (Okapi) over chunk tokens. No external dependency."""

    def __init__(self, chunks: list[Chunk]):
        self._chunk_ids = [chunk.chunk_id for chunk in chunks]
        doc_tokens = [tokenize(chunk.text) for chunk in chunks]
        doc_count = len(doc_tokens)
        self._avg_len = (
            sum(len(tokens) for tokens in doc_tokens) / doc_count if doc_count else 0.0
        )
        df: dict[str, int] = {}
        for tokens in doc_tokens:
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1
        self._idf = {
            term: math.log((doc_count - count + 0.5) / (count + 0.5) + 1.0)
            for term, count in df.items()
        }
        self._docs = [
            (self._term_counts(tokens), len(tokens)) for tokens in doc_tokens
        ]

    @staticmethod
    def _term_counts(tokens: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        return counts

    def search(self, query: str, top_n: int = 30) -> list[tuple[str, float]]:
        query_tokens = tokenize(query)
        scores = [0.0] * len(self._docs)
        for doc_index, (counts, doc_len) in enumerate(self._docs):
            if not doc_len:
                continue
            norm = 1.0 - BM25_B + BM25_B * doc_len / (self._avg_len or 1.0)
            score = 0.0
            for term in query_tokens:
                count = counts.get(term)
                if not count or term not in self._idf:
                    continue
                score += self._idf[term] * (
                    count * (BM25_K1 + 1.0)
                ) / (count + BM25_K1 * norm)
            scores[doc_index] = score
        ranked = sorted(
            enumerate(scores), key=lambda pair: pair[1], reverse=True
        )
        return [
            (self._chunk_ids[index], score)
            for index, score in ranked[:top_n]
            if score > 0.0
        ]


def rrf_fuse(
    rankings: list[list[str]],
    top_n: int = 30,
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """Weighted Reciprocal Rank Fusion: score(d) = Σ w_i / (k + rank_i).

    Plain RRF treats every ranking list as an equal voter. When the lists
    come from different queries (multi-query recall), an equal vote lets a
    rewritten query's noise outrank the original query's direct hit, so
    callers pass per-list weights — the original query out-votes rewrite
    expansions.
    """
    fused: dict[str, float] = {}
    for list_index, ranking in enumerate(rankings):
        weight = weights[list_index] if weights else 1.0
        for rank, chunk_id in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + rank)
    ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
    return ordered[:top_n]


def cosine(left: list[float], right: list[float]) -> float:
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if not norm_left or not norm_right:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


@dataclass
class EmbeddingClient:
    """OpenAI-compatible /embeddings client (SiliconFlow by default)."""

    environ: Mapping[str, str] | None = None
    transport: Callable[..., Any] = urlopen
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = 120
    batch_size: int = EMBED_BATCH_SIZE
    credential_env: str = ENV_EMBEDDING_API_KEY

    def _environment(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def configuration(self) -> tuple[str, str]:
        env = self._environment()
        api_key = env.get(self.credential_env, "").strip()
        if not api_key:
            raise MissingCredentialError(f"{self.credential_env} is not set")
        base_url = (self.base_url or env.get(ENV_EMBEDDING_BASE_URL, "").strip())
        if not base_url:
            base_url = DEFAULT_EMBEDDING_BASE_URL
        model = self.model or env.get(ENV_EMBEDDING_MODEL, "").strip()
        if not model:
            model = DEFAULT_EMBEDDING_MODEL
        if not base_url.startswith("https://"):
            raise ValidationError("embedding base_url must be an https URL")
        return base_url.rstrip("/"), model

    def embed(self, texts: list[str]) -> list[list[float]]:
        base_url, model = self.configuration()
        api_key = self._environment()[self.credential_env].strip()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            request = Request(
                f"{base_url}/embeddings",
                data=json.dumps(
                    {"model": model, "input": batch}, ensure_ascii=False
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with self.transport(request, timeout=self.timeout_seconds) as response:
                    status = getattr(response, "status", 200)
                    body = response.read()
            except HTTPError as exc:
                raise ExternalServiceError(
                    f"embedding endpoint returned HTTP {exc.code}"
                ) from exc
            except URLError as exc:
                raise ExternalServiceError(
                    f"embedding request failed: {exc.reason}"
                ) from exc
            if status < 200 or status >= 300:
                raise ExternalServiceError(
                    f"embedding endpoint returned HTTP {status}"
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExternalServiceError(
                    "embedding endpoint returned invalid JSON"
                ) from exc
            items = payload.get("data")
            if not isinstance(items, list) or len(items) != len(batch):
                raise ExternalServiceError(
                    "embedding endpoint returned an unexpected payload shape"
                )
            items.sort(key=lambda item: item.get("index", 0))
            for item in items:
                vector = item.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ExternalServiceError(
                        "embedding endpoint returned an empty embedding"
                    )
                vectors.append([float(value) for value in vector])
        return vectors


@dataclass
class RerankClient:
    """Cohere-style /rerank client for dedicated reranker models.

    SiliconFlow hosts free cross-encoder rerankers (bge-reranker-v2-m3) on
    the same OpenAI-compatible base URL and key as the embedding endpoint,
    so the rerank leg shares credentials by default. A purpose-built reranker
    scores query-passage pairs directly — cheaper and more consistent than
    asking the chat model to do listwise ranking, which remains the fallback
    when only an LLM is configured.
    """

    environ: Mapping[str, str] | None = None
    transport: Callable[..., Any] = urlopen
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = 60
    credential_env: str = ENV_EMBEDDING_API_KEY

    def _environment(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def configuration(self) -> tuple[str, str]:
        env = self._environment()
        api_key = env.get(self.credential_env, "").strip()
        if not api_key:
            raise MissingCredentialError(f"{self.credential_env} is not set")
        base_url = self.base_url or env.get(ENV_EMBEDDING_BASE_URL, "").strip()
        if not base_url:
            base_url = DEFAULT_EMBEDDING_BASE_URL
        model = self.model or env.get(ENV_RERANK_MODEL, "").strip()
        if not model:
            raise ConnectorNotConfiguredError(f"{ENV_RERANK_MODEL} is not set")
        if not base_url.startswith("https://"):
            raise ValidationError("rerank base_url must be an https URL")
        return base_url.rstrip("/"), model

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """Return (index, relevance_score) pairs, best first."""
        base_url, model = self.configuration()
        api_key = self._environment()[self.credential_env].strip()
        body: dict[str, Any] = {
            "model": model,
            "query": query,
            "documents": documents,
            "return_documents": False,
        }
        if top_n is not None:
            body["top_n"] = top_n
        request = Request(
            f"{base_url}/rerank",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with self.transport(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ExternalServiceError(
                f"rerank endpoint returned HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise ExternalServiceError(
                f"rerank request failed: {exc.reason}"
            ) from exc
        if status < 200 or status >= 300:
            raise ExternalServiceError(f"rerank endpoint returned HTTP {status}")
        results = payload.get("results")
        if not isinstance(results, list):
            raise ExternalServiceError(
                "rerank endpoint returned an unexpected payload shape"
            )
        scored: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict) or "index" not in item:
                continue
            scored.append((
                int(item["index"]),
                float(item.get("relevance_score", 0.0)),
            ))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored


class EmbeddingCache:
    """Content-hash cache of embedding vectors, persisted as JSONL.

    The corpus embedding pass is ~500 chunks; without the cache every query
    would re-pay it. Keyed by model + text hash so switching models or
    editing a chapter re-embeds exactly what changed.
    """

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, list[float]] = {}
        if self.path is not None and self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = record.get("key")
                vector = record.get("vector")
                if isinstance(key, str) and isinstance(vector, list):
                    self._entries[key] = [float(v) for v in vector]

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()

    def get(self, model: str, text: str) -> list[float] | None:
        return self._entries.get(self.key(model, text))

    def put(self, model: str, text: str, vector: list[float]) -> None:
        self._entries[self.key(model, text)] = vector
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"key": self.key(model, text), "vector": vector},
                    ensure_ascii=False,
                ) + "\n")


def default_llm(environ: Mapping[str, str] | None = None) -> Any | None:
    """The runtime's DeepSeek provider, or None when it is not configured.

    Retrieval works without an LLM; the LLM only upgrades individual steps
    (rewrite, rerank, grading, faithfulness). Returning None instead of
    raising keeps that story true at the call sites.
    """
    from .agents import DeepSeekResponsesProvider

    env = environ if environ is not None else os.environ
    provider = DeepSeekResponsesProvider(environ=env)
    try:
        provider.configuration()
    except (MissingCredentialError, ConnectorNotConfiguredError, ValidationError):
        return None
    return provider


@dataclass
class HybridRetriever:
    """The full chain. Construct cheaply; heavy work happens on first search."""

    dist_path: str | Path
    cache_path: str | Path | None = None
    environ: Mapping[str, str] | None = None
    transport: Callable[..., Any] = urlopen
    llm: Any | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        self._dist = Path(self.dist_path)
        self._chunks: list[Chunk] | None = None
        self._bm25: BM25Index | None = None
        self._vectors: dict[str, list[float]] = {}
        self._vector_norms: dict[str, float] = {}
        self._vectors_ready = False
        self._vector_degraded_reason: str | None = None

    # -- corpus --

    def corpus_chunks(self) -> list[Chunk]:
        if self._chunks is None:
            self._chunks = chunk_corpus(self._dist)
        return self._chunks

    def _chunk_by_id(self, chunk_id: str) -> Chunk | None:
        for chunk in self.corpus_chunks():
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def corpus_stats(self) -> dict[str, Any]:
        chunks = self.corpus_chunks()
        chapters = {chunk.chapter_id for chunk in chunks}
        return {
            "chunk_count": len(chunks),
            "chapter_count": len(chapters),
            "vector_backend": (
                "embedding_api" if self._vectors_ready else "disabled"
            ),
        }

    # -- index --

    def _ensure_bm25(self) -> BM25Index:
        if self._bm25 is None:
            self._bm25 = BM25Index(self.corpus_chunks())
        return self._bm25

    def _ensure_vectors(self) -> bool:
        if self._vectors_ready:
            return True
        if self._vector_degraded_reason is not None:
            return False
        client = EmbeddingClient(
            environ=self.environ,
            transport=self.transport,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            _, model = client.configuration()
        except (MissingCredentialError, ConnectorNotConfiguredError) as exc:
            self._vector_degraded_reason = str(exc)
            return False
        chunks = self.corpus_chunks()
        cache = EmbeddingCache(self._cache_path())
        missing = [
            chunk for chunk in chunks
            if cache.get(model, chunk.text) is None
        ]
        if missing:
            vectors = client.embed([chunk.text for chunk in missing])
            for chunk, vector in zip(missing, vectors):
                cache.put(model, chunk.text, vector)
        for chunk in chunks:
            vector = cache.get(model, chunk.text)
            if vector is None:
                self._vector_degraded_reason = "embedding cache miss"
                return False
            self._vectors[chunk.chunk_id] = vector
            self._vector_norms[chunk.chunk_id] = math.sqrt(
                sum(value * value for value in vector)
            )
        self._vectors_ready = True
        return True

    def _cache_path(self) -> Path:
        if self.cache_path is not None:
            return Path(self.cache_path)
        env = self.environ if self.environ is not None else os.environ
        return Path(env.get(ENV_RAG_CACHE, "") or DEFAULT_CACHE_DIR) / "embeddings.jsonl"

    def _query_vector(self, query: str) -> list[float] | None:
        client = EmbeddingClient(
            environ=self.environ,
            transport=self.transport,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            _, model = client.configuration()
            cache = EmbeddingCache(self._cache_path())
            vector = cache.get(model, query)
            if vector is None:
                vector = client.embed([query])[0]
                cache.put(model, query, vector)
            return vector
        except ExternalServiceError as exc:
            self._vector_degraded_reason = str(exc)
            return None

    # -- search --

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        candidate_k: int = 30,
        use_vectors: bool = True,
        rewrite: bool = False,
        rerank: bool = False,
        grade: bool = False,
        expand: bool = True,
    ) -> dict[str, Any]:
        """One query through the chain. Never raises for LLM problems."""
        warnings: list[str] = []
        rewritten_query: str | None = None
        alternate_query: str | None = None

        if rewrite and self.llm is not None:
            rewritten = self._llm_rewrite(query, warnings)
            if rewritten is None:
                warnings.append("query_rewrite_skipped")
            else:
                rewritten_query = rewritten.get("rewritten_query")
                alternate_query = rewritten.get("alternate_query")
        elif rewrite and self.llm is None:
            warnings.append("query_rewrite_unavailable_no_llm")

        # Stage architecture: recall (BM25+vector per query, RRF-fused) ->
        # rerank (precision) -> results. Rewrite semantics = CONCATENATION:
        # the rewrite's content keywords are appended to the original query.
        # History (see RAG_IMPROVEMENT_LOG.md for the full matrix): replacement
        # lost the original query's lexical anchors and let generic rewrite
        # words feed near-duplicate passages (prod-002 1.00->0.33); multi-query
        # fusion variants all let rerank noise and cross-leg interference erode
        # both kinds of wins (0.31-0.47 vs replacement's 0.675). Concatenation
        # keeps the original anchors while adding the rewrite's — the meta
        # questions get content keywords without the precise questions losing
        # theirs.
        effective_query = query
        if rewritten_query and rewritten_query.strip().lower() != query.strip().lower():
            effective_query = f"{query} {rewritten_query.strip()}".strip()
        rerank_backend = self.rerank_backend()
        leg_specs: list[tuple[str, float]] = [(effective_query, 1.0)]
        leg_rankings: list[list[str]] = []
        leg_weights: list[float] = []
        for leg_query, leg_weight in leg_specs:
            rankings, leg_warnings = self._fused_ranking(
                leg_query, candidate_k=candidate_k, use_vectors=use_vectors
            )
            warnings.extend(leg_warnings)
            leg_fused = rrf_fuse(rankings, top_n=max(candidate_k, top_k))
            if rerank and rerank_backend is not None:
                # The reranker always scores against the user's ORIGINAL
                # question, even when recall ran on a rewritten/concatenated
                # query: the rewrite's job is to widen the candidate pool,
                # not to redefine relevance. Measured: reranking the
                # concatenated query perturbed six otherwise-perfect cases
                # (1.00 -> 0.33-0.50) because the appended keywords steer
                # the cross-encoder; scoring against the original question
                # keeps the enriched pool but the original intent.
                reranked = self._rerank_candidates(
                    query, leg_fused, warnings, rerank_backend
                )
                if reranked is not None:
                    leg_fused = reranked
            leg_rankings.append([chunk_id for chunk_id, _score in leg_fused])
            leg_weights.append(leg_weight)
        fused = rrf_fuse(
            leg_rankings, top_n=max(candidate_k, top_k), weights=leg_weights
        )
        warnings = list(dict.fromkeys(warnings))

        retrieval_mode = "standard"
        if expand:
            relevant = None
            if grade and self.llm is not None:
                relevant = self._llm_grade(effective_query, fused, warnings)
                if relevant is None:
                    warnings.append("relevance_grading_skipped")
            elif grade and self.llm is None:
                warnings.append("relevance_grading_unavailable_no_llm")
            if relevant is not None and len(relevant) < MIN_RELEVANT_CHUNKS:
                warnings.append("corrective_second_pass_triggered")
                retrieval_mode = "expanded"
                second_query = (alternate_query or query).strip()
                second_rankings, second_warnings = self._fused_ranking(
                    second_query,
                    candidate_k=candidate_k * 2,
                    use_vectors=use_vectors,
                )
                warnings.extend(second_warnings)
                fused_map = dict(fused)
                for chunk_id, score in rrf_fuse(
                    second_rankings, top_n=candidate_k * 2
                ):
                    fused_map[chunk_id] = fused_map.get(chunk_id, 0.0) + score * 0.5
                for neighbor in self._neighbor_chunks(fused[:top_k]):
                    if neighbor not in fused_map:
                        fused_map[neighbor] = 0.01
                fused = sorted(
                    fused_map.items(), key=lambda pair: pair[1], reverse=True
                )

        results = []
        for rank, (chunk_id, score) in enumerate(fused[:top_k], start=1):
            chunk = self._chunk_by_id(chunk_id)
            if chunk is None:
                continue
            results.append({
                "rank": rank,
                "chunk_id": chunk.chunk_id,
                "chapter_id": chunk.chapter_id,
                "chapter_title": chunk.chapter_title,
                "heading": chunk.heading,
                "excerpt": chunk.text[:EXCERPT_CHARS],
                "score": round(score, 6),
            })
        mode = "hybrid" if self._vectors_ready and use_vectors else "bm25"
        return {
            "query": query,
            "effective_query": effective_query,
            "rewritten_query": rewritten_query,
            "alternate_query": alternate_query,
            "mode": mode,
            "retrieval_mode": retrieval_mode,
            "warnings": warnings,
            "vector_degraded_reason": self._vector_degraded_reason,
            "results": results,
        }

    def _fused_ranking(
        self, query: str, *, candidate_k: int, use_vectors: bool
    ) -> tuple[list[list[str]], list[str]]:
        warnings: list[str] = []
        bm25_hits = self._ensure_bm25().search(query, top_n=candidate_k)
        rankings = [[chunk_id for chunk_id, _score in bm25_hits]]
        if use_vectors and self._ensure_vectors():
            query_vector = self._query_vector(query)
            if query_vector is not None:
                scored = []
                for chunk in self.corpus_chunks():
                    vector = self._vectors.get(chunk.chunk_id)
                    if vector is None:
                        continue
                    norm = self._vector_norms.get(chunk.chunk_id) or 1.0
                    dot = sum(a * b for a, b in zip(query_vector, vector))
                    scored.append((chunk.chunk_id, dot / (norm or 1.0)))
                scored.sort(key=lambda pair: pair[1], reverse=True)
                rankings.append(
                    [chunk_id for chunk_id, _score in scored[:candidate_k]]
                )
            else:
                warnings.append("query_embedding_failed")
        elif use_vectors:
            warnings.append(
                f"vector_search_degraded: {self._vector_degraded_reason}"
            )
        return rankings, warnings

    def _neighbor_chunks(self, seed_ids: list[tuple[str, float]]) -> list[str]:
        neighbors: list[str] = []
        for chunk_id, _score in seed_ids:
            chunk = self._chunk_by_id(chunk_id)
            if chunk is None or chunk.chapter_id == "ontology__constraints":
                continue
            for position in (chunk.position - 1, chunk.position + 1):
                neighbor_id = f"{chunk.chapter_id}#{position}"
                if neighbor_id != chunk_id:
                    neighbors.append(neighbor_id)
        return neighbors

    # -- LLM steps --

    def _safe_llm(self, agent_name: str, instructions: str,
                  payload: dict[str, Any], schema: dict[str, Any],
                  warnings: list[str], warning_code: str) -> dict[str, Any] | None:
        try:
            return self.llm.complete(
                agent_name=agent_name,
                instructions=instructions,
                payload=payload,
                output_schema=schema,
                safety_identifier="opc-rag",
            )
        except Exception as exc:  # fail soft by design: retrieval must survive
            message = f"{warning_code}: {type(exc).__name__}: {exc}"
            warnings.append(message)
            log.warning("RAG LLM step skipped (%s): %s", agent_name, exc)
            return None

    def _llm_rewrite(
        self, query: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        return self._safe_llm(
            REWRITE_AGENT,
            "Rewrite the user query for hybrid retrieval over a cross-border "
            "e-commerce knowledge base written in Chinese with English platform "
            "terms. rewritten_query: the same intent, retrieval-friendly wording. "
            "alternate_query: a differently-worded second attempt for a corrective "
            "second pass. Return JSON only.",
            {"query": query},
            REWRITE_SCHEMA,
            warnings,
            "query_rewrite_failed",
        )

    def rerank_backend(self) -> str | None:
        """Which rerank implementation a rerank request would use."""
        env = self.environ if self.environ is not None else os.environ
        if env.get(ENV_RERANK_MODEL, "").strip():
            return "rerank_api"
        if self.llm is not None:
            return "llm"
        return None

    def _rerank_candidates(
        self,
        query: str,
        fused: list[tuple[str, float]],
        warnings: list[str],
        backend: str | None,
    ) -> list[tuple[str, float]] | None:
        """Dedicated reranker first; LLM listwise as fallback."""
        if backend == "rerank_api":
            ordered = self._api_rerank(query, fused, warnings)
            if ordered is not None:
                return ordered
        if self.llm is not None:
            return self._llm_rerank(query, fused, warnings)
        if backend == "rerank_api":
            return None  # api failed and no LLM fallback exists
        warnings.append("rerank_unavailable_no_reranker_no_llm")
        return None

    def _api_rerank(
        self,
        query: str,
        fused: list[tuple[str, float]],
        warnings: list[str],
    ) -> list[tuple[str, float]] | None:
        pool = fused[:RERANK_POOL]
        chunk_by_id = {chunk.chunk_id: chunk for chunk in self.corpus_chunks()}
        documents: list[str] = []
        pool_ids: list[str] = []
        for chunk_id, _score in pool:
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            # Chapter title leads the document so sibling constraints from
            # different platforms ("amazon.* vs walmart.* title length") get
            # platform context up front instead of burying it mid-text.
            documents.append(
                f"{chunk.chapter_title}\n{chunk.heading}\n{chunk.text[:1000]}"
            )
            pool_ids.append(chunk_id)
        if len(documents) < 2:
            return None
        client = RerankClient(
            environ=self.environ,
            transport=self.transport,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            scored = client.rerank(query, documents)
        except Exception as exc:  # fail soft: rerank must never break retrieval
            warnings.append(f"rerank_api_failed: {type(exc).__name__}: {exc}")
            log.warning("RAG rerank API step skipped: %s", exc)
            return None
        ordered: list[tuple[str, float]] = []
        seen: set[str] = set()
        for index, _score in scored:
            if not 0 <= index < len(pool_ids):
                continue
            chunk_id = pool_ids[index]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            ordered.append((chunk_id, fused_score(fused, chunk_id)))
        for chunk_id, score in fused:
            if chunk_id not in seen:
                ordered.append((chunk_id, score))
        return ordered

    def _llm_rerank(
        self, query: str, fused: list[tuple[str, float]], warnings: list[str]
    ) -> list[tuple[str, float]] | None:
        pool = fused[:RERANK_POOL]
        chunk_by_id = {chunk.chunk_id: chunk for chunk in self.corpus_chunks()}
        passages = []
        for index, (chunk_id, _score) in enumerate(pool):
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            passages.append({
                "index": index,
                "heading": chunk.heading,
                "excerpt": chunk.text[:300],
            })
        if len(passages) < 2:
            return None
        result = self._safe_llm(
            RERANK_AGENT,
            "Rank the passages by relevance to the query, most relevant first. "
            "Return 'ranking' containing every passage index exactly once. "
            "Return JSON only.",
            {"query": query, "passages": passages},
            RERANK_SCHEMA,
            warnings,
            "rerank_failed",
        )
        if result is None:
            return None
        ranking = result.get("ranking")
        if not isinstance(ranking, list) or sorted(
            idx for idx in ranking if isinstance(idx, int)
        ) != list(range(len(passages))):
            warnings.append("rerank_invalid_output")
            return None
        ordered: list[tuple[str, float]] = []
        for index in ranking:
            chunk_id = pool[index][0]
            ordered.append((chunk_id, fused_score(fused, chunk_id)))
        seen = {chunk_id for chunk_id, _score in ordered}
        for chunk_id, score in fused:
            if chunk_id not in seen:
                ordered.append((chunk_id, score))
        return ordered

    def _llm_grade(
        self,
        query: str,
        fused: list[tuple[str, float]],
        warnings: list[str],
    ) -> list[str] | None:
        pool = fused[:GRADE_POOL]
        chunk_by_id = {chunk.chunk_id: chunk for chunk in self.corpus_chunks()}
        passages = []
        for index, (chunk_id, _score) in enumerate(pool):
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            passages.append({
                "index": index,
                "excerpt": chunk.text[:250],
            })
        if not passages:
            return None
        result = self._safe_llm(
            GRADE_AGENT,
            "Decide which passages contain evidence relevant to the query. "
            "Return 'relevant_indices' listing the indices of relevant passages; "
            "an empty list is a valid answer. Return JSON only.",
            {"query": query, "passages": passages},
            GRADE_SCHEMA,
            warnings,
            "relevance_grading_failed",
        )
        if result is None:
            return None
        indices = result.get("relevant_indices")
        if not isinstance(indices, list):
            warnings.append("relevance_grading_invalid_output")
            return None
        return [
            pool[index][0] for index in indices
            if isinstance(index, int) and 0 <= index < len(pool)
        ]

    # -- generation-side helpers (used by the eval runner and tools) --

    def generate_answer(self, question: str, contexts: list[dict[str, Any]],
                        warnings: list[str]) -> dict[str, Any] | None:
        if self.llm is None:
            warnings.append("generation_unavailable_no_llm")
            return None
        return self._safe_llm(
            GENERATE_AGENT,
            "Answer the question using only the supplied context passages. Cite "
            "the chunk_id values you used in source_ids. If the passages do not "
            "contain the answer, say so explicitly. Return JSON only.",
            {"question": question, "contexts": contexts},
            GENERATE_SCHEMA,
            warnings,
            "generation_failed",
        )

    def check_faithfulness(
        self,
        question: str,
        answer: str,
        contexts: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any] | None:
        """ragas-style faithfulness: decompose the answer into claims and
        verify each against the retrieved context. Score = supported / total."""
        if self.llm is None:
            warnings.append("faithfulness_unavailable_no_llm")
            return None
        result = self._safe_llm(
            FAITHFULNESS_AGENT,
            "Decompose the answer into atomic factual claims and verify each "
            "claim against the supplied context passages. supported is true only "
            "when a passage explicitly states the claim. Return JSON only.",
            {"question": question, "answer": answer, "contexts": contexts},
            FAITHFULNESS_SCHEMA,
            warnings,
            "faithfulness_failed",
        )
        if result is None:
            return None
        claims = result.get("claims")
        if not isinstance(claims, list) or not claims:
            warnings.append("faithfulness_invalid_output")
            return None
        supported = sum(
            1 for claim in claims
            if isinstance(claim, dict) and claim.get("supported") is True
        )
        return {
            "score": supported / len(claims),
            "claim_count": len(claims),
            "supported_count": supported,
            "claims": claims,
        }


def fused_score(fused: list[tuple[str, float]], chunk_id: str) -> float:
    for candidate_id, score in fused:
        if candidate_id == chunk_id:
            return score
    return 0.0


def substring_baseline(chunks: list[Chunk], query: str,
                       top_n: int = 30) -> list[tuple[str, float]]:
    """The pre-RAG baseline: term-occurrence scoring, MCP search_knowledge's
    approach. Kept here so the eval runner can measure what hybrid retrieval
    buys over it with zero LLM or embedding cost."""
    terms = set(tokenize(query))
    if not terms:
        return []
    scored: list[tuple[str, float]] = []
    for chunk in chunks:
        haystack = " ".join(tokenize(chunk.text))
        score = sum(1.0 for term in terms if term in haystack)
        if score > 0.0:
            scored.append((chunk.chunk_id, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]
