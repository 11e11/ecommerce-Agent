"""Stage-one feedback: versioned requirements, full rerun, scoped merge.

No incremental execution or long-term memory. Structured checks establish
scope/budget invariants; semantic correctness is still reviewed by the LLM.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ExternalServiceError, ValidationError

CATEGORIES = ['advertising', 'inventory', 'other']
UPDATE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['needs_clarification', 'question', 'target_platform', 'categories',
                 'preserve_platforms', 'forbid_budget_increase'],
    'properties': {
        'needs_clarification': {'type': 'boolean'}, 'question': {'type': 'string'},
        'target_platform': {'type': 'string'},
        'categories': {'type': 'array', 'items': {'type': 'string', 'enum': CATEGORIES}, 'uniqueItems': True},
        'preserve_platforms': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True},
        'forbid_budget_increase': {'type': 'boolean'},
    },
}


def initial_state(objective: str, platforms: list[str]) -> dict:
    return {'version': 1, 'objective': objective,
            'platforms': [p for p in platforms if p != 'cross_platform'],
            'budget_caps': {}, 'latest_update': None}


def update_state(previous: dict, update: dict, feedback: str) -> dict:
    if list(Draft202012Validator(UPDATE_SCHEMA).iter_errors(update)):
        raise ExternalServiceError('requirement update does not match schema')
    if update['needs_clarification']:
        if not update['question'].strip():
            raise ExternalServiceError('clarification question is empty')
        return {'status': 'needs_clarification', 'question': update['question']}
    platform = update['target_platform']
    if platform not in previous['platforms'] or not update['categories']:
        raise ValidationError('revision requires one existing platform and at least one category')
    if any(p not in previous['platforms'] or p == platform for p in update['preserve_platforms']):
        raise ValidationError('invalid preserved platform')
    result = deepcopy(previous)
    result['version'] += 1
    # An omitted budget restriction never revokes a still-effective restriction.
    if update['forbid_budget_increase']:
        result['budget_caps'][platform] = 'no_increase'
    result['latest_update'] = {**deepcopy(update), 'feedback': feedback}
    return result


def report_schema(schema: dict) -> dict:
    result = deepcopy(schema)
    item = result['properties']['priorities']['items']
    props = {
        'category': {'type': 'string', 'enum': CATEGORIES},
        'budget_change': {'type': 'string', 'enum': ['increase', 'no_increase', 'not_applicable']},
    }
    item['properties'].update(props)
    item['required'] += list(props)
    return result


def assign_advice_ids(report: dict) -> dict:
    counts: dict[str, int] = {}
    for item in report['priorities']:
        if item.get('category') not in CATEGORIES or item.get('budget_change') not in {
            'increase', 'no_increase', 'not_applicable'}:
            raise ExternalServiceError('priority is missing structured revision metadata')
        stem = '+'.join(sorted(item['platforms'])) + ':' + item['category']
        counts[stem] = counts.get(stem, 0) + 1
        item['advice_id'] = f'{stem}:{counts[stem]}'
    return report


def scoped_merge(parent: dict, candidate: dict, state: dict) -> dict:
    """Only replace existing priorities in the explicitly editable scope.

    Preserved items are deep-copied exactly, including ranks and IDs. Refuse
    ambiguous cross-platform items and cardinality changes in this first stage.
    """
    update = state['latest_update']
    target = update['target_platform']
    categories = set(update['categories'])
    def editable(item: dict) -> bool:
        return target in item['platforms'] and item['category'] in categories
    old_items = parent['priorities']
    for item in old_items:
        if editable(item) and item['platforms'] != [target]:
            raise ValidationError('cross-platform priority needs an explicit revised scope')
    wanted = [p for p in old_items if editable(p)]
    available = [deepcopy(p) for p in candidate['priorities'] if editable(p)]
    if not wanted:
        raise ValidationError('no existing priority matches the requested revision scope')
    merged = deepcopy(candidate)
    priorities = []
    for old in old_items:
        if not editable(old):
            priorities.append(deepcopy(old))
            continue
        match = next((p for p in available if p['category'] == old['category']), None)
        if match is None:
            raise ExternalServiceError('manager omitted a requested replacement priority')
        available.remove(match)
        match.update(advice_id=old['advice_id'], rank=old['rank'])
        priorities.append(match)
    merged['priorities'] = priorities
    # These fields are fresh synthesis and are independently semantically reviewed.
    merged['requirement_version'] = state['version']
    merged['revision_changes'] = [
        {'advice_id': p['advice_id'], 'status': 'updated' if editable(p) else 'preserved'}
        for p in priorities]
    return merged


def rule_checks(parent: dict, report: dict, state: dict) -> list[dict]:
    update = state['latest_update']
    target, categories = update['target_platform'], set(update['categories'])
    before = {p['advice_id']: p for p in parent['priorities']}
    after = {p['advice_id']: p for p in report['priorities']}
    editable = {k for k,p in before.items() if p['platforms'] == [target] and p['category'] in categories}
    preserved = set(before) - editable
    # Changed content must be user-visible, not just a budget metadata relabel.
    fields = ('title', 'why_now', 'downstream_action', 'expected_impact')
    updated = bool(editable) and all(k in after and any(before[k].get(f) != after[k].get(f) for f in fields) for k in editable)
    scope = set(before) == set(after) and updated and all(before[k] == after.get(k) for k in preserved)
    budget = all(not (any(p in state['budget_caps'] for p in item['platforms']) and item['budget_change'] == 'increase') for item in after.values())
    return [{'name': 'revision_scope', 'passed': scope}, {'name': 'budget_constraint', 'passed': budget}]
