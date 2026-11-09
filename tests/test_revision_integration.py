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


def test_full_rerun_with_scoped_merge_and_idempotent_update(tmp_path):
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
    child = app.agent_runs.follow_up(owner, first['id'], '只修改 Amazon 库存，广告不加预算，Shopify保持', 'child', 'child')
    before = len(provider.calls)
    assert app.agent_runs.follow_up(owner, first['id'], '只修改 Amazon 库存，广告不加预算，Shopify保持', 'child', 'child')['id'] == child['id']
    assert len(provider.calls) == before
    bundle = app.agent_runs.execute(owner, child['id'], 'execute-child')
    revised = next(a['content'] for a in bundle['artifacts'] if a['kind'] == 'weekly_ops_report')
    assert revised['priorities'][0] == original['priorities'][0]
    assert revised['priorities'][1]['title'] != original['priorities'][1]['title']
    assert revised['priorities'][2:] == original['priorities'][2:]
    checks = next(a['content']['checks'] for a in bundle['artifacts'] if a['kind'] == 'revision_rule_checks')
    assert all(c['passed'] for c in checks)
    # Stage one deliberately reruns both Specialists.
    names = {name for name, kind, _ in provider.calls[before:] if kind == 'specialist'}
    assert {'platform_amazon_operator', 'platform_shopify_operator'} <= names
