"""RAG evaluation runner: FlashRAG-style retrieval metrics + faithfulness.

Two conventions are borrowed deliberately so the numbers mean something
beyond this repo:

- Retrieval quality uses FlashRAG's definitions: Recall@k is the mean of
  |top-k retrieved ∩ gold| / |gold|, MRR@k the mean reciprocal rank of the
  first relevant hit within the top k.
- Faithfulness follows ragas: decompose a generated answer into atomic
  claims and count the share a retrieved context explicitly supports.

The gold standard here is chapter + heading anchored (``gold_chapter``
required, ``gold_heading`` optional substring). Cases whose gold matches no
chunk in the corpus are flagged ``gold_unverified`` in the report — that is
a dataset bug to fix, not a retrieval failure, and it is surfaced rather
than silently averaged away.

The runner is deliberately cheap to invoke: ``limit``/``ids`` restrict how
many QA items run, and the generation + faithfulness leg only executes when
``generate`` is set and an LLM is configured. Retrieval metrics never
require an LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationError
from .retrieval import Chunk, HybridRetriever, substring_baseline

REQUIRED_QA_FIELDS = ("id", "question", "gold_chapter")


def load_qa(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate the YAML QA set. Bad rows fail loudly."""
    qa_path = Path(path)
    if not qa_path.is_file():
        raise ValidationError(f"QA file not found: {qa_path}")
    try:
        data = yaml.safe_load(qa_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationError(f"QA file is not valid YAML: {exc}") from exc
    if not isinstance(data, list):
        raise ValidationError("QA file must be a YAML list of case objects")
    seen_ids: set[str] = set()
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValidationError(f"QA row {position} is not an object")
        for field_name in REQUIRED_QA_FIELDS:
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"QA row {position} is missing required field '{field_name}'"
                )
        case_id = item["id"]
        if case_id in seen_ids:
            raise ValidationError(f"QA row {position} duplicates id '{case_id}'")
        seen_ids.add(case_id)
    return data


def chunk_is_relevant(chunk: Chunk, case: dict[str, Any]) -> bool:
    # Constraint-backed cases keep a tight gold: the packaged constraint chunk
    # itself, plus a chapter section only when the case pins a heading. Letting
    # the whole source chapter count as gold would dilute Recall@k to noise —
    # a rich chapter easily spans thirty chunks. ``gold_constraints`` (list)
    # admits sibling constraints that state the same fact for the same
    # section: evidence the retriever legitimately surfaces should not count
    # as a miss just because the dataset author pinned one id of several.
    gold_ids: list[str] = []
    if case.get("gold_constraint"):
        gold_ids.append(str(case["gold_constraint"]))
    for extra in case.get("gold_constraints") or []:
        if isinstance(extra, str) and extra.strip():
            gold_ids.append(extra.strip())
    if gold_ids:
        if chunk.chunk_id in {f"constraint__{gid}" for gid in gold_ids}:
            return True
        if chunk.chapter_id != case["gold_chapter"]:
            return False
        gold_heading = case.get("gold_heading")
        return bool(
            isinstance(gold_heading, str)
            and gold_heading.strip()
            and gold_heading.strip().lower() in chunk.heading.lower()
        )
    if chunk.chapter_id != case["gold_chapter"]:
        return False
    gold_heading = case.get("gold_heading")
    if isinstance(gold_heading, str) and gold_heading.strip():
        return gold_heading.strip().lower() in chunk.heading.lower()
    return True


def recall_at_k(ranked_ids: list[str], relevance, k: int,
                gold_count: int) -> float:
    """FlashRAG definition: |top-k ∩ gold| / |gold| for this query.

    ``gold_count`` must be the size of the full gold set in the corpus,
    counted independently of what was retrieved — taking it from the ranked
    list would silently inflate the score.
    """
    if gold_count == 0:
        return 0.0
    hits = sum(1 for chunk_id in ranked_ids[:k] if relevance(chunk_id))
    return hits / gold_count


def mrr_at_k(ranked_ids: list[str], relevance, k: int) -> float:
    for rank, chunk_id in enumerate(ranked_ids[:k], start=1):
        if relevance(chunk_id):
            return 1.0 / rank
    return 0.0


def select_cases(qa_items: list[dict[str, Any]], *, limit: int | None = None,
                 ids: list[str] | None = None) -> list[dict[str, Any]]:
    if ids:
        wanted = [item for item in qa_items if item["id"] in set(ids)]
        missing = set(ids) - {item["id"] for item in wanted}
        if missing:
            raise ValidationError(f"unknown QA ids: {sorted(missing)}")
        return wanted
    return qa_items[:limit] if limit is not None else qa_items


def _case_report(
    retriever: HybridRetriever,
    case: dict[str, Any],
    *,
    top_k: int,
    generate: bool,
    rewrite: bool = False,
    rerank: bool = False,
) -> dict[str, Any]:
    question = case["question"]
    chunks = retriever.corpus_chunks()
    relevance = lambda chunk_id: (  # noqa: E731 - tiny closure, kept local
        (chunk := _find_chunk(retriever, chunk_id)) is not None
        and chunk_is_relevant(chunk, case)
    )
    gold_in_corpus = sum(1 for chunk in chunks if chunk_is_relevant(chunk, case))

    hybrid = retriever.search(
        question, top_k=top_k, use_vectors=True,
        rewrite=rewrite, rerank=rerank, grade=False, expand=False,
    )
    hybrid_ids = [result["chunk_id"] for result in hybrid["results"]]
    baseline_hits = substring_baseline(chunks, question, top_n=top_k)
    baseline_ids = [chunk_id for chunk_id, _score in baseline_hits]

    report: dict[str, Any] = {
        "id": case["id"],
        "category": case.get("category"),
        "question": question,
        "rewritten_query": hybrid.get("rewritten_query"),
        "gold_chapter": case["gold_chapter"],
        "gold_heading": case.get("gold_heading"),
        "gold_chunks_in_corpus": gold_in_corpus,
        "gold_unverified": gold_in_corpus == 0,
        "mode": hybrid["mode"],
        "warnings": hybrid["warnings"],
        "vector_degraded_reason": hybrid["vector_degraded_reason"],
        "hybrid_top": hybrid_ids[:5],
        "baseline_top": baseline_ids[:5],
        "hybrid_recall_at_3": round(
            recall_at_k(hybrid_ids, relevance, 3, gold_in_corpus), 4
        ),
        "hybrid_mrr_at_10": round(mrr_at_k(hybrid_ids, relevance, 10), 4),
        "baseline_recall_at_3": round(
            recall_at_k(baseline_ids, relevance, 3, gold_in_corpus), 4
        ),
        "baseline_mrr_at_10": round(mrr_at_k(baseline_ids, relevance, 10), 4),
    }
    if generate:
        contexts = [
            {
                "source": result["chunk_id"],
                "heading": result["heading"],
                "text": result["excerpt"],
            }
            for result in hybrid["results"][:5]
        ]
        warnings: list[str] = []
        generated = retriever.generate_answer(question, contexts, warnings)
        if generated is not None:
            report["answer"] = generated.get("answer")
            report["source_ids"] = generated.get("source_ids")
            faithfulness = retriever.check_faithfulness(
                question, str(generated.get("answer", "")), contexts, warnings
            )
            if faithfulness is not None:
                report["faithfulness"] = {
                    "score": round(faithfulness["score"], 4),
                    "claim_count": faithfulness["claim_count"],
                    "supported_count": faithfulness["supported_count"],
                }
        report["generation_warnings"] = warnings
    return report


def _find_chunk(retriever: HybridRetriever, chunk_id: str) -> Chunk | None:
    for chunk in retriever.corpus_chunks():
        if chunk.chunk_id == chunk_id:
            return chunk
    return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def run_eval(
    retriever: HybridRetriever,
    qa_items: list[dict[str, Any]],
    *,
    top_k: int = 10,
    limit: int | None = None,
    ids: list[str] | None = None,
    generate: bool = False,
    rewrite: bool = False,
    rerank: bool = False,
) -> dict[str, Any]:
    """Run the QA set (or a slice of it) through retrieval and report."""
    cases = select_cases(qa_items, limit=limit, ids=ids)
    if not cases:
        raise ValidationError("no QA cases selected")
    case_reports = [
        _case_report(
            retriever, case, top_k=max(top_k, 10), generate=generate,
            rewrite=rewrite, rerank=rerank,
        )
        for case in cases
    ]
    verified = [
        case for case in case_reports if not case["gold_unverified"]
    ]

    def _collect(report_key: str) -> list[float]:
        return [
            case[report_key] for case in verified
            if isinstance(case.get(report_key), (int, float))
        ]

    hybrid_recall = _collect("hybrid_recall_at_3")
    hybrid_mrr = _collect("hybrid_mrr_at_10")
    baseline_recall = _collect("baseline_recall_at_3")
    baseline_mrr = _collect("baseline_mrr_at_10")
    faithfulness_scores = [
        case["faithfulness"]["score"] for case in case_reports
        if isinstance(case.get("faithfulness"), dict)
    ]
    stats = retriever.corpus_stats()
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": stats,
        "config": {
            "case_count": len(case_reports),
            "top_k": max(top_k, 10),
            "generate": generate,
            "rewrite": rewrite,
            "rerank": rerank,
            "rerank_backend": retriever.rerank_backend(),
        },
        "metrics": {
            "hybrid": {
                "recall_at_3": _mean(hybrid_recall),
                "mrr_at_10": _mean(hybrid_mrr),
            },
            "baseline": {
                "recall_at_3": _mean(baseline_recall),
                "mrr_at_10": _mean(baseline_mrr),
            },
            "faithfulness": {
                "score": _mean(faithfulness_scores),
                "status": (
                    "ok" if faithfulness_scores else
                    ("skipped" if generate else "not_requested")
                ),
            },
        },
        "gold_unverified_count": sum(
            1 for case in case_reports if case["gold_unverified"]
        ),
        "cases": case_reports,
    }
    return report


def write_report(report: dict[str, Any], out_path: str | Path | None) -> None:
    if out_path is None:
        return
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
