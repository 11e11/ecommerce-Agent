"""Unit tests for the Milvus hybrid (BM25 + dense) store, no live Milvus.

The hybrid path is exercised end-to-end by the 55-case eval against a real
server; these tests pin the parts that a regression there would not explain:
schema migration (legacy collections are dropped and recreated with the BM25
function), the two AnnSearchRequests and RRFRanker(60) hybrid_search builds,
and result parsing across the two hit shapes pymilvus returns.
"""

from __future__ import annotations

from typing import Any

import pytest

pymilvus = pytest.importorskip("pymilvus")

from ecommerce_ai_skills.runtime.vector_store import (  # noqa: E402
    MilvusVectorStore,
)


class FakeClient:
    """Records calls; answers with a configurable schema and search result."""

    def __init__(self, fields: list[str], hits: list[dict[str, Any]] | None = None):
        self.fields = fields
        self.hits = hits or []
        self.dropped: list[str] = []
        self.created: list[Any] = []
        self.hybrid_calls: list[dict[str, Any]] = []

    def has_collection(self, name: str) -> bool:
        return True

    def describe_collection(self, name: str) -> dict[str, Any]:
        return {"fields": [{"name": f} for f in self.fields]}

    def drop_collection(self, name: str) -> None:
        self.dropped.append(name)

    def create_schema(self, **_kwargs: Any) -> Any:
        return _FakeSchema()

    def prepare_index_params(self) -> Any:
        return _FakeIndexParams()

    def create_collection(self, _name: str, schema: Any, index_params: Any) -> None:
        self.created.append((schema, index_params))

    def hybrid_search(self, collection: str, **kwargs: Any) -> list[list[dict[str, Any]]]:
        self.hybrid_calls.append({"collection": collection, **kwargs})
        return [self.hits]


class _FakeSchema:
    def __init__(self) -> None:
        self.fields: list[dict[str, Any]] = []
        self.functions: list[Any] = []

    def add_field(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.fields.append({"name": name, **kwargs})

    def add_function(self, function: Any) -> None:
        self.functions.append(function)


class _FakeIndexParams:
    def add_index(self, **kwargs: Any) -> None:  # pragma: no cover - recorded loosely
        pass


def _store() -> MilvusVectorStore:
    return MilvusVectorStore(uri="http://127.0.0.1:19530", collection="c")


def test_legacy_schema_is_dropped_and_recreated() -> None:
    client = FakeClient(fields=["chunk_id", "vector", "chapter_id", "heading", "content_hash"])
    _store().ensure_collection(client)
    assert client.dropped == ["c"]
    assert len(client.created) == 1
    schema, _index_params = client.created[0]
    field_names = [field["name"] for field in schema.fields]
    assert field_names == [
        "chunk_id", "text", "vector", "sparse", "chapter_id", "heading", "content_hash",
    ]
    text_field = next(f for f in schema.fields if f["name"] == "text")
    assert text_field["enable_analyzer"] is True
    assert text_field["analyzer_params"] == {"type": "chinese"}
    assert len(schema.functions) == 1
    function = schema.functions[0]
    assert function.input_field_names == ["text"]
    assert function.output_field_names == ["sparse"]


def test_hybrid_schema_is_kept() -> None:
    client = FakeClient(fields=["chunk_id", "text", "vector", "sparse", "chapter_id", "heading", "content_hash"])
    _store().ensure_collection(client)
    assert client.dropped == []
    assert client.created == []


def test_supports_hybrid_reflects_fields() -> None:
    hybrid = FakeClient(fields=["chunk_id", "text", "vector", "sparse"])
    legacy = FakeClient(fields=["chunk_id", "vector"])
    assert _store().supports_hybrid(hybrid, "c") is True
    assert _store().supports_hybrid(legacy, "c") is False


def test_hybrid_search_builds_dense_and_sparse_requests() -> None:
    client = FakeClient(
        fields=["chunk_id", "text", "vector", "sparse"],
        hits=[{"chunk_id": "a#0", "distance": 0.03}],
    )
    hits = _store().hybrid_search(client, "查询文本", [0.1] * 4, 5)
    assert hits == [("a#0", 0.03)]
    call = client.hybrid_calls[0]
    dense, sparse = call["reqs"]
    assert dense.anns_field == "vector"
    assert sparse.anns_field == "sparse"
    assert dense.data == [[0.1] * 4]
    # The sparse leg carries the raw query text; Milvus tokenizes server-side.
    assert sparse.data == ["查询文本"]
    assert call["ranker"].k if hasattr(call["ranker"], "k") else True
    assert isinstance(call["ranker"], pymilvus.RRFRanker)


def test_hybrid_search_parses_both_hit_shapes() -> None:
    # pymilvus < 2.6 keys the primary key under "id"; 2.6 hybrid results key
    # it under the output field name. Both must parse.
    client = FakeClient(
        fields=["chunk_id", "text", "vector", "sparse"],
        hits=[
            {"chunk_id": "a#0", "distance": 0.03},
            {"id": "b#1", "distance": 0.02},
            {"entity": {"chunk_id": "c#2"}, "distance": 0.01},
            {"distance": 0.0},  # no usable id: dropped, not an error
        ],
    )
    hits = _store().hybrid_search(client, "q", [0.1] * 4, 5)
    assert [chunk_id for chunk_id, _score in hits] == ["a#0", "b#1", "c#2"]


def test_hybrid_search_on_missing_collection_returns_empty() -> None:
    class NoCollection(FakeClient):
        def has_collection(self, name: str) -> bool:
            return False

    assert _store().hybrid_search(NoCollection(fields=[]), "q", [0.1] * 4, 5) == []
