"""Conservative, deterministic Specialist reuse for scoped feedback runs."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from .evidence_policy import DEFAULT_FRESHNESS_DAYS, FRESHNESS_DAYS_BY_SOURCE_TYPE


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def dependencies(run, spec, task_state, contracts, model, audit):
    evidence = sorted((s for s in run['evidence'] if s['platform'] in {spec.platform, 'cross_platform'}), key=lambda s:s['source_id'])
    gaps = sorted((g for g in (audit or {}).get('gaps', []) if g.get('platform') == spec.platform), key=lambda g:g['id'])
    fields = {'tenant': run['tenant_id'], 'platform': spec.platform,
              'objective': (task_state or {}).get('objective', run['objective']),
              'budget_constraint': (task_state or {}).get('budget_caps', {}).get(spec.platform),
              'evidence': evidence, 'skills': contracts, 'instructions': spec.instructions,
              'policy': spec.tool_policy, 'model': model, 'graph_hash': run.get('graph_version_hash'),
              'gaps': gaps}
    return {'fingerprint': digest(fields), 'platform': spec.platform,
            'evidence_ids': [s['source_id'] for s in evidence]}


def result_artifact(bundle, name, kind):
    task = next((t for t in bundle['tasks'] if t['agent_name'] == name), None)
    if not task:
        return None
    return next((a['content'] for a in reversed(bundle['artifacts'])
                 if a.get('task_id') == task['id'] and a['kind'] == kind
                 and a.get('attempt') == task['attempt_count']), None)


def decide(parent, run, spec, state, current, now=None):
    """No LLM judgment; a missing or uncertain dependency means recompute."""
    now = now or datetime.now(timezone.utc)
    if spec.platform == state['latest_update']['target_platform']:
        return None, 'target_modified'
    if parent['run']['tenant_id'] != run['tenant_id']:
        return None, 'tenant_mismatch'
    if parent['run']['status'] != 'completed' or parent['run']['review_status'] != 'approved':
        return None, 'parent_not_approved'
    task = next((t for t in parent['tasks'] if t['agent_name']==spec.name), None)
    if not task or task['status'] != 'completed':
        return None, 'task_not_completed'
    if any(e['event_type']=='task.tool_call' and e.get('task_id')==task['id'] for e in parent.get('events', [])):
        return None, 'tool_context_not_revalidated'
    stored = result_artifact(parent, spec.name, 'specialist_dependencies')
    result = result_artifact(parent, spec.name, 'specialist_finding')
    if not stored or not result:
        return None, 'missing_dependency_snapshot'
    if stored['fingerprint'] != current['fingerprint']:
        return None, 'dependencies_changed'
    if stored.get('result_digest') != digest(result):
        return None, 'result_integrity_changed'
    if result.get('evidence_sufficiency', {}).get('level') not in {'sufficient','partial'}:
        return None, 'unusable_evidence_status'
    if stored.get('cross_platform_dependency'):
        return None, 'cross_platform_dependency'
    # Only immutable run inputs can be verified without another external read.
    refs = {ref for item in result.get('findings', []) for ref in item.get('evidence_refs', [])}
    if result.get('knowledge_evidence') or not refs <= set(current['evidence_ids']):
        return None, 'external_evidence_not_revalidated'
    for source in run['evidence']:
        if source['source_id'] not in current['evidence_ids']:
            continue
        try:
            stamp = datetime.fromisoformat(source['observed_at'].replace('Z','+00:00'))
            age = (now - stamp).total_seconds()
            maximum = FRESHNESS_DAYS_BY_SOURCE_TYPE.get(source['source_type'], DEFAULT_FRESHNESS_DAYS)*86400
            if stamp.tzinfo is None or age < 0 or age > maximum:
                return None, 'evidence_expired_or_future'
        except (ValueError, TypeError):
            return None, 'invalid_evidence_time'
    return deepcopy(result), 'dependencies_valid'
