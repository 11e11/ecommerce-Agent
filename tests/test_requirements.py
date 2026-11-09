from copy import deepcopy

import pytest

from ecommerce_ai_skills.runtime import requirements as r
from ecommerce_ai_skills.runtime.errors import ExternalServiceError, ValidationError


def update(**changes):
    return dict(needs_clarification=False, question='', target_platform='amazon',
                categories=['inventory'], preserve_platforms=['shopify'],
                forbid_budget_increase=True, **changes)


def state():
    return r.update_state(r.initial_state('compare goods', ['amazon', 'shopify']), update(), '只修改库存')


def report():
    return {'priorities': [
        {'rank': 1, 'advice_id': 'amazon:advertising:1', 'platforms': ['amazon'], 'category': 'advertising', 'budget_change': 'no_increase', 'title': 'check advertising'},
        {'rank': 2, 'advice_id': 'amazon:inventory:1', 'platforms': ['amazon'], 'category': 'inventory', 'budget_change': 'not_applicable', 'title': 'check inventory'},
        {'rank': 3, 'advice_id': 'shopify:inventory:1', 'platforms': ['shopify'], 'category': 'inventory', 'budget_change': 'not_applicable', 'title': 'keep this'},
    ]}


def test_scoped_merge_and_rules():
    original = report()
    candidate = deepcopy(original)
    candidate['priorities'][0]['title'] = 'unwanted advertising rewrite'
    candidate['priorities'][1]['title'] = 'first check sellable stock'
    candidate['priorities'][2]['title'] = 'unwanted other platform rewrite'
    merged = r.scoped_merge(original, candidate, state())
    assert merged['priorities'][0] == original['priorities'][0]
    assert merged['priorities'][2] == original['priorities'][2]
    assert all(c['passed'] for c in r.rule_checks(original, merged, state()))
    assert original == report()


def test_rule_checks_detect_no_change_and_budget_increase():
    original = report()
    assert not r.rule_checks(original, original, state())[0]['passed']
    changed = deepcopy(original)
    changed['priorities'][1].update(title='new', budget_change='increase')
    assert not r.rule_checks(original, changed, state())[1]['passed']


def test_requirements_version_and_restriction_persistence():
    prior = state()
    patch = update()
    patch['forbid_budget_increase'] = False
    next_state = r.update_state(prior, patch, '改库存')
    assert next_state['version'] == 3
    assert next_state['budget_caps'] == {'amazon': 'no_increase'}
    assert prior['version'] == 2


def test_ambiguous_feedback_returns_clarification():
    patch = update()
    patch.update(needs_clarification=True, question='请说明需要修改哪一部分？')
    assert r.update_state(state(), patch, '不好')['status'] == 'needs_clarification'


def test_invalid_platform_and_fabricated_fields_rejected():
    patch = update()
    patch['target_platform'] = 'unavailable'
    with pytest.raises(ValidationError):
        r.update_state(state(), patch, 'change')
    patch['invented'] = True
    with pytest.raises(ExternalServiceError):
        r.update_state(state(), patch, 'change')


def test_missing_replacement_and_cross_platform_item_rejected():
    with pytest.raises(ExternalServiceError):
        r.scoped_merge(report(), {'priorities': []}, state())
    original = report()
    original['priorities'][1]['platforms'] = ['amazon', 'shopify']
    with pytest.raises(ValidationError):
        r.scoped_merge(original, original, state())


def test_platform_context_excludes_other_platform_report():
    from ecommerce_ai_skills.runtime.continuations import context_for_platform
    ctx = context_for_platform({'parent_report': report(), 'task_state': state(),
                                'previous_findings': [{'platform': 'shopify'}, {'platform': 'amazon'}]}, 'amazon')
    assert 'parent_report' not in ctx
    assert ctx['previous_findings'] == [{'platform': 'amazon'}]
