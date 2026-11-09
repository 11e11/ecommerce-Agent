"""Export one saved evaluation's role outputs without issuing model calls."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ecommerce_ai_skills.runtime.storage import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--case', default='held-01-stage_one')
    args = parser.parse_args()
    folder = ROOT / 'operations/revision-eval'
    record = json.loads((folder / (args.case + '.json')).read_text(encoding='utf-8'))
    db_path = ROOT / args.db
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    db = Database(db_path)
    with db.connect() as conn:
        row = conn.execute('SELECT tenant_id FROM agent_runs WHERE id=?', (record['run_id'],)).fetchone()
    if row is None:
        raise ValueError('case run does not exist in the selected database')
    bundle = db.get_agent_run_bundle(row['tenant_id'], record['run_id'])
    context = db.get_continuation_context(row['tenant_id'], record['run_id']) or {}
    result = {'case_id': args.case, 'run_id': record['run_id'],
              'feedback': record['feedback'], 'task_state': context.get('task_state'),
              'agents': [{'name': t['agent_name'], 'role': t['role'], 'status': t['status'],
                          'result': t.get('result')} for t in bundle['tasks']],
              'checks': [a['content'] for a in bundle['artifacts'] if a['kind']=='revision_rule_checks'],
              'loop_events': [{'type': e['event_type'], 'payload': e['payload']} for e in bundle['events']
                              if e['event_type'].startswith('task.loop.')],
              'note': 'Real LLM role outputs. Evaluation tool permissions are empty; this demonstrates feedback handling, not retrieval quality.'}
    path = folder / ('example-' + args.case + '.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
