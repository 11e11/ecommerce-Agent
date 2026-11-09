from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ecommerce_ai_skills.runtime import reuse


def fixture():
    now = datetime.now(timezone.utc)
    spec = SimpleNamespace(name='platform_shopify_operator', platform='shopify', instructions='local only', tool_policy={}, skill_ids=())
    run = {'tenant_id':'tenant', 'objective':'compare products', 'graph_version_hash':'graph',
           'evidence':[{'source_id':'shopify', 'platform':'shopify','source_type':'shopify_products',
                        'observed_at':now.isoformat(), 'data':{'stock':80}}]}
    state = {'objective':'compare products','budget_caps':{}, 'latest_update':{'target_platform':'amazon'}}
    current = reuse.dependencies(run,spec,state,[],('fixture','v1'),{})
    result = {'evidence_sufficiency':{'level':'partial'}, 'findings':[{'evidence_refs':['shopify']}], 'knowledge_evidence':[]}
    stored = dict(current, result_digest=reuse.digest(result), cross_platform_dependency=False)
    parent = {'run':dict(run,status='completed',review_status='approved'),
              'tasks':[{'id':'t','agent_name':spec.name,'status':'completed','attempt_count':1}],
              'artifacts':[{'task_id':'t','kind':kind,'attempt':1,'content':content} for kind,content in
                           [('specialist_dependencies',stored),('specialist_finding',result)]]}
    return parent,run,spec,state,current,now


def test_valid_partial_result_is_reused_without_hiding_its_gaps():
    args = fixture()
    result, reason = reuse.decide(*args)
    assert reason == 'dependencies_valid'
    assert result['evidence_sufficiency']['level'] == 'partial'
    result['findings'].clear()
    assert args[0]['artifacts'][1]['content']['findings']


@pytest.mark.parametrize('field,value', [('data',{'stock':1}),('observed_at','2026-01-01T00:00:00+00:00')])
def test_changed_input_invalidates_fingerprint(field,value):
    parent,run,spec,state,current,now = fixture()
    run['evidence'][0][field] = value
    changed = reuse.dependencies(run,spec,state,[],('fixture','v1'),{})
    assert reuse.decide(parent,run,spec,state,changed,now)[1] == 'dependencies_changed'


def test_expiry_cross_dependency_external_evidence_and_bad_status_fall_back():
    parent,run,spec,state,current,now = fixture()
    assert reuse.decide(parent,run,spec,state,current,now+timedelta(days=4))[1] == 'evidence_expired_or_future'
    stored = parent['artifacts'][0]['content']
    stored['cross_platform_dependency'] = True
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'cross_platform_dependency'
    stored['cross_platform_dependency'] = False
    parent['artifacts'][1]['content']['knowledge_evidence'] = [{'source_id':'dynamic'}]
    stored['result_digest'] = reuse.digest(parent['artifacts'][1]['content'])
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'external_evidence_not_revalidated'
    parent['run']['review_status'] = 'rejected'
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'parent_not_approved'


def test_tenant_model_constraints_and_integrity_are_checked():
    parent,run,spec,state,current,now = fixture()
    run['tenant_id'] = 'other'
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'tenant_mismatch'
    run['tenant_id'] = 'tenant'
    changed = reuse.dependencies(run,spec,state,[],('fixture','v2'),{})
    assert reuse.decide(parent,run,spec,state,changed,now)[1] == 'dependencies_changed'
    changed_state = deepcopy(state)
    changed_state['budget_caps']['shopify'] = 'no_increase'
    changed = reuse.dependencies(run,spec,changed_state,[],('fixture','v1'),{})
    assert reuse.decide(parent,run,spec,changed_state,changed,now)[1] == 'dependencies_changed'
    parent['artifacts'][1]['content']['findings'] = []
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'result_integrity_changed'


def test_uncited_tool_context_also_forces_recompute():
    parent,run,spec,state,current,now = fixture()
    parent['events'] = [{'event_type':'task.tool_call','task_id':'t'}]
    assert reuse.decide(parent,run,spec,state,current,now)[1] == 'tool_context_not_revalidated'
