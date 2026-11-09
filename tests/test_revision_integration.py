from copy import deepcopy
from datetime import datetime, timezone

import agent_outputs

from ecommerce_ai_skills.runtime.api import RuntimeApplication
from ecommerce_ai_skills.runtime.storage import Database
from test_harness_integration import Provider, OpsClient


class RevisionProvider(Provider):
    def complete(self, *, agent_name, payload, output_schema, **kwargs):
        if agent_name == 'requirement_router':
            self.calls.append((agent_name, 'requirements', payload))
            return {'needs_clarification': False, 'question': '', 'target_platform': 'amazon',
                    'categories': ['inventory'], 'preserve_platforms': ['shopify'], 'forbid_budget_increase': True}
        result = super().complete(agent_name=agent_name, payload=payload, output_schema=output_schema, **kwargs)
        if agent_outputs.schema_kind(output_schema) == 'manager':
            item = result['priorities'][0]
            result['priorities'] = []
            for platform in ('amazon', 'shopify'):
                for category in ('advertising', 'inventory'):
                    row = deepcopy(item)
                    row.update(rank=len(result['priorities']) + 1, platforms=[platform],
                               category=category, budget_change='no_increase' if category == 'advertising' else 'not_applicable',
                               evidence_refs=[platform], knowledge_citations=[],
                               title=platform + category + str((payload.get('current_task_state') or {}).get('version', 1)))
                    result['priorities'].append(row)
        return result


import pytest


@pytest.mark.parametrize('mode', ['full','selective','missing_snapshot'])
def test_full_rerun_with_scoped_merge_and_idempotent_update(tmp_path, mode):
    provider = RevisionProvider()
    app = RuntimeApplication(Database(tmp_path / 'revision.sqlite'), agent_provider=provider)
    owner = app.auth.authenticate(app.bootstrap('Revision', 'owner@example.test')['api_key'])
    app.agent_runs.knowledge_client = OpsClient()
    sources = [{'source_id': p, 'platform': p, 'source_type': kind,
                'observed_at': datetime.now(timezone.utc).isoformat(), 'data': {'title': 'fan'}}
               for p, kind in [('amazon', 'amazon_listing'), ('shopify', 'shopify_products')]]
    first = app.agent_runs.request(owner, 'weekly_ops', '优化 Amazon Shopify listing title', sources,
                                   'initial', 'initial', revision_support=True)
    parent = app.agent_runs.execute(owner, first['id'], 'execute')
    original = next(a['content'] for a in parent['artifacts'] if a['kind'] == 'weekly_ops_report')
    if mode == 'missing_snapshot':
        with app.db.transaction() as conn:
            conn.execute("DELETE FROM agent_artifacts WHERE run_id=? AND kind='specialist_dependencies'", (first['id'],))
    execution_mode = 'full' if mode == 'full' else 'selective'
    child = app.agent_runs.follow_up(owner, first['id'], '只修改 Amazon 库存，广告不加预算，Shopify保持', 'child', 'child', execution_mode=execution_mode)
    before = len(provider.calls)
    assert app.agent_runs.follow_up(owner, first['id'], '只修改 Amazon 库存，广告不加预算，Shopify保持', 'child', 'child', execution_mode=execution_mode)['id'] == child['id']
    assert len(provider.calls) == before
    bundle = app.agent_runs.execute(owner, child['id'], 'execute-child')
    revised = next(a['content'] for a in bundle['artifacts'] if a['kind'] == 'weekly_ops_report')
    assert revised['priorities'][0] == original['priorities'][0]
    assert revised['priorities'][1]['title'] != original['priorities'][1]['title']
    assert revised['priorities'][2:] == original['priorities'][2:]
    checks = next(a['content']['checks'] for a in bundle['artifacts'] if a['kind'] == 'revision_rule_checks')
    assert all(c['passed'] for c in checks)
    names = {name for name, kind, _ in provider.calls[before:] if kind == 'specialist'}
    if mode == 'selective':
        assert 'platform_amazon_operator' in names and 'platform_shopify_operator' not in names
        reused = [e for e in bundle['events'] if e['event_type']=='task.reused']
        assert len(reused) == 1 and reused[0]['payload']['agent_name']=='platform_shopify_operator'
        task = next(t for t in bundle['tasks'] if t['agent_name']=='platform_shopify_operator')
        assert task['attempt_count'] == 0 and task['started_at'] is None
    else:
        assert {'platform_amazon_operator', 'platform_shopify_operator'} <= names


def test_demo_http_keeps_legacy_behavior_and_cannot_claim_real_revision_support(tmp_path):
    from fastapi.testclient import TestClient
    from ecommerce_ai_skills.demo_seed import DemoSeedProvider
    from ecommerce_ai_skills.runtime.api import create_app
    app = RuntimeApplication(Database(tmp_path / 'demo.sqlite'), agent_provider=DemoSeedProvider())
    credentials = app.bootstrap('Demo compatibility', 'demo@example.test')
    source = {'source_id':'product', 'platform':'amazon', 'source_type':'amazon_listing',
              'observed_at':datetime.now(timezone.utc).isoformat(), 'data':{'title':'sample'}}
    with TestClient(create_app(app)) as client:
        headers = {'Authorization':'Bearer '+credentials['api_key'], 'Idempotency-Key':'legacy-demo'}
        body = {'workflow':'weekly_ops','objective':'优化 Amazon listing title','evidence':[source]}
        response = client.post('/v1/agent-runs', headers=headers, json=body)
        assert response.status_code == 200
        assert client.post('/v1/agent-runs/'+response.json()['id']+'/execute', headers=headers).status_code == 200
        body['revision_support'] = True
        response = client.post('/v1/agent-runs', headers=headers, json=body)
        assert response.status_code == 422
