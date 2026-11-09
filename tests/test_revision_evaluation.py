import importlib.util
from pathlib import Path

import yaml


def test_small_dataset_is_one_scenario_with_disjoint_dev_and_test_ids():
    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / 'tests/revision-cases.yaml').read_text(encoding='utf-8'))
    assert len(data['cases']) == 6
    assert len({c['id'] for c in data['cases']}) == 6
    assert sum(c['split'] == 'development' for c in data['cases']) == 2
    assert sum(c['split'] == 'held_out' for c in data['cases']) == 4
    assert data['expected_update']['categories'] == ['inventory']
    assert {r['platform'] for r in data['evidence']} == {'amazon','shopify'}


def test_missing_or_failed_results_cannot_inflate_pass_rates():
    path = Path(__file__).resolve().parents[1] / 'scripts/summarize_revision_eval.py'
    spec = importlib.util.spec_from_file_location('summarizer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [{'status':'completed','rule_checks':[{'name':'revision_scope','passed':True},
              {'name':'budget_constraint','passed':True}], 'semantic_judge':{'passed':True},
              'model_calls':10}, {'status':'error'}]
    result = module.aggregate(rows)
    assert result['cases'] == 2
    assert result['scope_passed'] == result['semantic_passed'] == 1
    assert result['errors'] == 1
    assert result['median_model_calls'] == 10
