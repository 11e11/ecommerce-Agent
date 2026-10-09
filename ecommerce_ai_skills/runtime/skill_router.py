"""Deterministic, auditable Skill selection for Weekly Ops runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationError


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _terms(query: str) -> set[str]:
    normalized = _norm(query)
    terms = set(re.findall(r"[a-z0-9]{3,}", normalized))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        for length in (2, 3, 4):
            terms.update(run[i:i + length] for i in range(len(run) - length + 1))
    return terms


def _effort_tier(
    selected: list[str], marketplace_platforms: list[str]
) -> tuple[str, str]:
    """Deterministic effort tier driving role topology and per-task budgets.

    The tier is computed from the routing result alone -- never by a model --
    so the same objective always gets the same编制 and the tier can be compared
    against outcomes later. Rules are deliberately conservative (prefer a
    heavier tier when in doubt): under-provisioning a complex objective costs
    more than one extra retrieval round.
    """
    multi_intent = len(selected) > 1
    if (
        len(marketplace_platforms) >= 3
        or len(selected) >= 3
        or ("ecom-applicability" in selected and multi_intent)
    ):
        return (
            "deep",
            f"{len(marketplace_platforms)} marketplace(s), {len(selected)} skill(s), "
            f"multi_intent={multi_intent}",
        )
    if len(marketplace_platforms) == 1 and len(selected) == 1:
        return (
            "simple",
            f"single marketplace, single skill ({selected[0]})",
        )
    return (
        "standard",
        f"{len(marketplace_platforms)} marketplace(s), {len(selected)} skill(s), "
        f"multi_intent={multi_intent}",
    )


class SkillRouter:
    """Use the installed manifest triggers and constraint coverage, as MCP does."""

    def __init__(self, skills_root: Path, ontology: dict[str, Any]):
        self.skills_root = skills_root
        self.constraints = ontology.get("constraints", [])
        self.manifests: dict[str, dict[str, Any]] = {}
        for path in sorted(skills_root.glob("*/manifest.yaml")):
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if manifest.get("name") != path.parent.name:
                raise ValidationError(f"invalid installed skill manifest: {path.parent.name}")
            self.manifests[path.parent.name] = manifest

    def _coverage(self, skill_id: str, query: str) -> int | None:
        used = set(self.manifests[skill_id].get("uses_constraints") or [])
        if not used:
            return None
        haystacks = []
        for constraint in self.constraints:
            if constraint.get("id") not in used:
                continue
            statement = constraint.get("statement") or {}
            haystacks.append(_norm(" ".join(str(value) for value in (
                constraint.get("id", ""), constraint.get("attribute", ""),
                constraint.get("value", ""), statement.get("zh", ""),
                statement.get("en", ""),
            ))))
        return sum(any(term in haystack for haystack in haystacks) for term in _terms(query))

    def select(self, objective: str, platforms: list[str]) -> dict[str, Any]:
        query = _norm(objective)
        scores = []
        for skill_id, manifest in self.manifests.items():
            triggers = manifest.get("triggers") or {}
            score = sum(bool(keyword and _norm(keyword) in query)
                        for keyword in triggers.get("keywords", []))
            score += 2 * sum(bool(re.search(pattern, query, re.IGNORECASE))
                             for pattern in triggers.get("patterns", []))
            if score:
                scores.append((skill_id, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        if not scores:
            raise ValidationError("Skill Router found no matching skill for the objective")
        ranked = dict(scores)
        applicability = ranked.get("ecom-applicability", 0)
        domains = [(skill, score) for skill, score in scores if skill != "ecom-applicability"]
        best_skill, best_score = domains[0] if domains else (None, 0)
        if applicability >= 2 and applicability >= best_score:
            best_skill, best_score = "ecom-applicability", applicability
        elif best_skill is None and applicability:
            best_skill, best_score = "ecom-applicability", applicability
        if best_skill is None:
            raise ValidationError("Skill Router found no matching domain skill")
        best_coverage = self._coverage(best_skill, objective)
        if best_coverage == 0 or (best_coverage is None and best_score < 2):
            raise ValidationError("Skill Router match has insufficient constraint coverage")
        selected = [best_skill]
        coverage = {best_skill: best_coverage}
        for skill, score in scores:
            if skill == best_skill or score < 2 or best_score - score >= 2:
                continue
            skill_coverage = self._coverage(skill, objective)
            if skill_coverage == 0 or (skill_coverage is None and score < 2):
                continue
            selected.append(skill)
            coverage[skill] = skill_coverage
        by_platform = {}
        for platform in platforms:
            if platform == "cross_platform":
                continue
            matched = [skill for skill in selected if not self.manifests[skill].get("platforms")
                       or platform in self.manifests[skill]["platforms"]]
            if not matched:
                raise ValidationError(f"Skill Router found no selected skill for {platform}")
            by_platform[platform] = matched
        marketplace_platforms = [platform for platform in platforms if platform != "cross_platform"]
        effort_tier, effort_rationale = _effort_tier(selected, marketplace_platforms)
        return {"query": objective, "skills": selected, "by_platform": by_platform,
                "scores": {skill: ranked[skill] for skill in selected},
                "constraint_coverage": coverage,
                "effort_tier": effort_tier,
                "effort_rationale": effort_rationale}
