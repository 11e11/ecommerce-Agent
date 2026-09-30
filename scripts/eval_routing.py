#!/usr/bin/env python3
"""Routing accuracy evaluation: baseline vs weighted skill router.

Measures the routing-accuracy claim on the version-controlled, labeled case
suites (152 cases over 9 skills):

  Router A (baseline)  简单关键词匹配 + 固定优先级
                       raw substring match, no whitespace/case folding,
                       regex patterns ignored, first hit wins in a fixed
                       (alphabetical skill-id) priority order.
  Router B (weighted)  the shipped router (integration/mcp-server.py
                       _route_query): whitespace/case folded, keyword=1 +
                       regex=2 scoring, alphabetical tie-break, applicability
                       arbitration. Falls back to a verify_all.py R1-replica
                       if the dist package cannot be loaded.

Sanity gate: Router B must reproduce verify_all.py R1's zero-misroute result
on tests/routing-cases.yaml, otherwise this script exits non-zero.

Usage:
  python scripts/eval_routing.py [--json PATH]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"
CASE_FILES = {
    "fixed": ROOT / "tests" / "routing-cases.yaml",
    "natural": ROOT / "tests" / "routing-cases-natural.yaml",
}
APPLICABILITY = "ecom-applicability"


def normalize(text: str) -> str:
    """Whitespace-fold + lowercase, identical to verify_all.py R1 / mcp _norm."""
    return re.sub(r"\s+", "", text).lower()


def load_rules() -> dict[str, dict]:
    rules: dict[str, dict] = {}
    for mf_path in sorted(SKILLS_DIR.glob("*/manifest.yaml")):
        mf = yaml.safe_load(mf_path.read_text(encoding="utf-8"))
        sid = mf.get("name") or mf_path.parent.name
        triggers = mf.get("triggers") or {}
        rules[sid] = {
            "keywords": [str(k) for k in (triggers.get("keywords") or [])],
            "patterns": [str(p) for p in (triggers.get("patterns") or [])],
        }
    if not rules:
        raise SystemExit(f"no skill manifests found under {SKILLS_DIR}")
    return rules


def load_cases(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        cases = yaml.safe_load(fh) or []
    out = []
    for i, case in enumerate(cases):
        query = str(case.get("query", "")).strip()
        expect = str(case.get("expect", "")).strip()
        if query and expect:
            out.append({"idx": i + 1, "query": query, "expect": expect})
    return out


class BaselineRouter:
    """简单关键词匹配 + 固定优先级：原始子串、无归一化、忽略正则、字母序首中即胜。"""

    def __init__(self, rules: dict[str, dict]):
        self.rules = rules
        self.priority = sorted(rules)

    def route(self, query: str) -> str | None:
        for sid in self.priority:
            if any(kw and kw in query for kw in self.rules[sid]["keywords"]):
                return sid
        return None


class WeightedRouterR1:
    """verify_all.py R1 同口径副本：归一化 + 关键词1分 + 正则2分 + applicability 仲裁。"""

    def __init__(self, rules: dict[str, dict]):
        self.rules = rules
        self.app = rules.get(APPLICABILITY, {"keywords": [], "patterns": []})

    def route(self, query: str) -> str | None:
        qn = normalize(query)
        app_keywords = self.app["keywords"]
        app_patterns = self.app["patterns"]
        app_score = sum(1 for kw in app_keywords if len(kw) >= 3 and normalize(kw) in qn)
        app_score += 2 * sum(
            1 for p in app_patterns if re.search(p, qn, re.IGNORECASE)
        )
        best_match, best_score = None, 0
        for sid, rule in self.rules.items():
            if sid == APPLICABILITY:
                continue
            score = sum(1 for kw in rule["keywords"] if normalize(kw) in qn)
            score += 2 * sum(
                1 for p in rule["patterns"] if re.search(p, qn, re.IGNORECASE)
            )
            if score > best_score:
                best_score, best_match = score, sid
        if app_score >= 2 and app_score >= best_score:
            best_match = APPLICABILITY
        return best_match


class ShippedRouterAdapter:
    """Adapt OPCServer._route_query (JSON out) to the .route() interface."""

    def __init__(self, server):
        self.server = server

    def route(self, query: str) -> str | None:
        data = json.loads(self.server._route_query(query))
        return data.get("skill")


def load_shipped_router():
    """Load the real MCP server router; return None when it cannot be loaded."""
    script = ROOT / "integration" / "mcp-server.py"
    dist = ROOT / "dist"
    if not script.exists() or not dist.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("opc_mcp_server", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return ShippedRouterAdapter(mod.OPCServer(dist))
    except Exception as exc:
        print(
            f"  [warn] shipped MCP router unavailable ({type(exc).__name__}: {exc});"
            " falling back to R1-replica weighted router"
        )
        return None


def evaluate(router, cases: list[dict]) -> dict:
    detail = []
    for case in cases:
        got = router.route(case["query"])
        detail.append({**case, "got": got, "ok": got == case["expect"]})
    correct = sum(1 for d in detail if d["ok"])
    return {
        "total": len(detail),
        "correct": correct,
        "accuracy": (correct / len(detail)) if detail else 0.0,
        "detail": detail,
    }


def per_skill(detail: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for d in detail:
        slot = stats.setdefault(d["expect"], {"total": 0, "correct": 0})
        slot["total"] += 1
        slot["correct"] += 1 if d["ok"] else 0
    for slot in stats.values():
        slot["accuracy"] = slot["correct"] / slot["total"] if slot["total"] else 0.0
    return stats


def confusion(detail: list[dict]) -> Counter:
    return Counter((d["expect"], d["got"]) for d in detail if not d["ok"])


def fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="write full results as JSON")
    args = parser.parse_args()

    rules = load_rules()
    suites = {name: load_cases(path) for name, path in CASE_FILES.items()}

    baseline = BaselineRouter(rules)
    shipped = load_shipped_router()
    if shipped is not None:
        weighted_router, weighted_impl = shipped, "shipped MCP router (_route_query, dist/)"
    else:
        weighted_router, weighted_impl = WeightedRouterR1(rules), "R1-replica (verify_all.py 口径)"

    print("=== 路由分发准确率评测 ===")
    print(f"技能数: {len(rules)}  触发词: "
          f"{sum(len(r['keywords']) for r in rules.values())} 关键词 / "
          f"{sum(len(r['patterns']) for r in rules.values())} 正则")
    print("路由器:")
    print("  A baseline : 简单关键词匹配 + 固定优先级 (原始子串、无归一化、忽略正则、字母序首中即胜)")
    print(f"  B weighted : {weighted_impl}")

    results: dict[str, dict] = {}
    for suite, cases in suites.items():
        results[suite] = {
            "baseline": evaluate(baseline, cases),
            "weighted": evaluate(weighted_router, cases),
        }

    print("\n--- 数据集构成 (期望技能分布) ---")
    for suite, cases in suites.items():
        dist = Counter(c["expect"] for c in cases)
        dist_s = ", ".join(f"{sid}={n}" for sid, n in sorted(dist.items()))
        print(f"  {suite:8s} {len(cases):4d} 条  ({dist_s})")

    print("\n--- 准确率对比 ---")
    print(f"  {'suite':8s} {'n':>4s}  {'baseline':>16s}  {'weighted':>16s}  {'delta':>8s}")
    for suite in suites:
        base, weighted = results[suite]["baseline"], results[suite]["weighted"]
        delta = (weighted["accuracy"] - base["accuracy"]) * 100
        print(f"  {suite:8s} {base['total']:4d}  "
              f"{fmt_pct(base['accuracy']):>7s} ({base['correct']:3d})  "
              f"{fmt_pct(weighted['accuracy']):>7s} ({weighted['correct']:3d})  "
              f"{delta:+.1f}pp")
    all_cases = [c for cases in suites.values() for c in cases]
    combined_base = evaluate(baseline, all_cases)
    combined_weighted = evaluate(weighted_router, all_cases)
    print(f"  {'total':8s} {combined_base['total']:4d}  "
          f"{fmt_pct(combined_base['accuracy']):>7s} ({combined_base['correct']:3d})  "
          f"{fmt_pct(combined_weighted['accuracy']):>7s} ({combined_weighted['correct']:3d})  "
          f"{(combined_weighted['accuracy'] - combined_base['accuracy']) * 100:+.1f}pp")

    for suite in suites:
        for impl in ("baseline", "weighted"):
            misses = [d for d in results[suite][impl]["detail"] if not d["ok"]]
            if not misses:
                continue
            print(f"\n--- 错路由明细 [{suite} / {impl}] 共 {len(misses)} 条 ---")
            for d in misses[:20]:
                print(f"  #{d['idx']:<4d} expect={d['expect']:<22s} got={str(d['got']):<22s} {d['query'][:46]}")
            if len(misses) > 20:
                print(f"  ... 其余 {len(misses) - 20} 条见 JSON 输出")

    for suite in suites:
        base = {d["idx"]: d for d in results[suite]["baseline"]["detail"]}
        weighted = {d["idx"]: d for d in results[suite]["weighted"]["detail"]}
        flips = [d for d in weighted.values() if not base[d["idx"]]["ok"] and d["ok"]]
        regressions = [d for d in weighted.values() if base[d["idx"]]["ok"] and not d["ok"]]
        print(f"\n--- 翻转分析 [{suite}] ---")
        print(f"  基线错→加权对: {len(flips)} 条" + (" (前 8 条)" if len(flips) > 8 else ""))
        for d in flips[:8]:
            print(f"    #{d['idx']:<4d} {d['expect']:<22s} 基线→{str(base[d['idx']]['got']):<20s} {d['query'][:42]}")
        print(f"  基线对→加权错(回归): {len(regressions)} 条")
        for d in regressions[:8]:
            print(f"    #{d['idx']:<4d} expect={d['expect']:<22s} got={str(d['got']):<22s} {d['query'][:40]}")

    print("\n--- 分技能准确率 (全部用例合并) ---")
    base_skill = per_skill(combined_base["detail"])
    weighted_skill = per_skill(combined_weighted["detail"])
    print(f"  {'skill':24s} {'n':>4s}  {'baseline':>8s}  {'weighted':>8s}")
    for sid in sorted(base_skill):
        b = base_skill[sid]
        w = weighted_skill.get(sid, {"accuracy": 0.0, "total": 0})
        print(f"  {sid:24s} {b['total']:4d}  {fmt_pct(b['accuracy']):>8s}  {fmt_pct(w['accuracy']):>8s}")

    print("\n--- 基线混淆对 Top (expect -> got) ---")
    for (expect, got), n in confusion(combined_base["detail"]).most_common(8):
        print(f"  {expect} -> {got}: {n}")

    fixed_weighted = results["fixed"]["weighted"]
    gate_ok = fixed_weighted["correct"] == fixed_weighted["total"]
    print("\n--- 一致性校验 ---")
    print(f"  weighted @ fixed = {fixed_weighted['correct']}/{fixed_weighted['total']}"
          f" (verify_all.py R1 门禁要求 0 错路由): {'[ok]' if gate_ok else '[FAIL]'}")

    if args.json:
        payload = {
            "routers": {
                "baseline": "keyword substring + alphabetical fixed priority (no normalization, no regex)",
                "weighted": weighted_impl,
            },
            "skills": len(rules),
            "suites": {},
        }
        for suite in suites:
            payload["suites"][suite] = {
                impl: {
                    "total": results[suite][impl]["total"],
                    "correct": results[suite][impl]["correct"],
                    "accuracy": results[suite][impl]["accuracy"],
                    "detail": results[suite][impl]["detail"],
                }
                for impl in ("baseline", "weighted")
            }
        payload["combined"] = {
            "baseline": {"total": combined_base["total"], "correct": combined_base["correct"],
                         "accuracy": combined_base["accuracy"]},
            "weighted": {"total": combined_weighted["total"], "correct": combined_weighted["correct"],
                         "accuracy": combined_weighted["accuracy"]},
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 结果已写入: {args.json}")

    return 0 if gate_ok else 2


if __name__ == "__main__":
    sys.exit(main())
