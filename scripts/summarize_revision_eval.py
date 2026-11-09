"""Summarize rule and LLM judgments separately; missing/error cases fail."""
import argparse
import json
from pathlib import Path
import statistics

import yaml


def aggregate(rows):
    n = len(rows)
    def passed(name):
        return sum(any(c['name'] == name and c['passed'] for c in r.get('rule_checks', [])) for r in rows)
    def median(name):
        values = [r[name] for r in rows if r.get('status') == 'completed' and name in r]
        return round(statistics.median(values), 1) if values else None
    return {'cases': n, 'scope_passed': passed('revision_scope'), 'budget_passed': passed('budget_constraint'),
            'semantic_passed': sum(r.get('semantic_judge', {}).get('passed', False) for r in rows),
            'review_approved': sum(r.get('review_status') == 'approved' for r in rows),
            'errors': sum(r.get('status') == 'error' for r in rows),
            'median_model_calls': median('model_calls'), 'median_tool_calls': median('tool_calls'),
            'median_input_tokens': median('input_tokens'), 'median_output_tokens': median('output_tokens'),
            'median_latency_ms': median('latency_ms')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='operations/revision-eval')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / args.input
    fixture = yaml.safe_load((root/'tests/revision-cases.yaml').read_text(encoding='utf-8'))
    rows = []
    missing = []
    for case in fixture['cases']:
        for variant in ('baseline','stage_one'):
            path = folder / f"{case['id']}-{variant}.json"
            if path.exists():
                rows.append(json.loads(path.read_text(encoding='utf-8')))
            else:
                missing.append(path.name)
                rows.append({'case_id':case['id'],'split':case['split'],'variant':variant,'status':'error','error':'missing'})
    groups = {}
    contracts = {r.get('execution_contract_hash') for r in rows if r.get('execution_contract_hash')}
    if len(contracts) > 1:
        raise ValueError('Cannot compare results from different execution contracts')
    for split in ('development','held_out','all'):
        groups[split] = {v: aggregate([r for r in rows if r['variant']==v and (split=='all' or r['split']==split)])
                         for v in ('baseline','stage_one')}
    lines = ['# 基础阶段反馈修订评测', '',
             '一个固定业务案例，六种反馈表达：2 条开发、4 条保留测试。所有变体使用同一份真实模型首轮报告。', '',
             '基线：文本反馈追加后完整重跑，统一输出字段但不做结构化需求更新或范围合并。基础阶段：解释需求、完整重跑、按范围合并、规则检查与模型审核。', '',
             '以下通过数由规则读取结构化报告计算；语义合格由另一次 LLM 调用判定，不等于人工真值。错误或缺失结果计为未通过。', '',
             '| 集合 | 变体 | 范围正确 | 预算字段遵守 | LLM语义合格 | 系统审核通过 | 执行错误 |',
             '|---|---|---|---|---|---|---|']
    for split, variants in groups.items():
        for variant, g in variants.items():
            split_label = {'development':'开发', 'held_out':'保留测试', 'all':'全部'}[split]
            variant_label = {'baseline':'文本反馈基线', 'stage_one':'基础阶段'}[variant]
            lines.append(f"| {split_label} | {variant_label} | {g['scope_passed']}/{g['cases']} | {g['budget_passed']}/{g['cases']} | {g['semantic_passed']}/{g['cases']} | {g['review_approved']}/{g['cases']} | {g['errors']} |")
    lines += ['', '## 执行开销', '', '工作流开销包含需求解释，排除单独的评测评分调用与共享首轮生成。token 是服务返回的已知用量；传输中断可能无法取得用量。基础阶段不做局部重算，不预期必然降本。', '',
              '| 变体 | 模型调用中位数 | 工具调用中位数 | 输入token中位数 | 输出token中位数 | 耗时中位数(ms) |',
              '|---|---|---|---|---|---|']
    for v,g in groups['all'].items():
        lines.append(f"| {v} | {g['median_model_calls']} | {g['median_tool_calls']} | {g['median_input_tokens']} | {g['median_output_tokens']} | {g['median_latency_ms']} |")
    lines += ['', '## 逐条结果', '', '| 用例 | 变体 | 规则 | 语义 | 说明 |', '|---|---|---|---|---|']
    for r in rows:
        checks = ', '.join(f"{c['name']}={c['passed']}" for c in r.get('rule_checks',[])) or '未完成'
        reason = str(r.get('semantic_judge',{}).get('reason') or r.get('error','')).replace('|','/').replace('\n',' ')
        lines.append(f"| {r['case_id']} | {r['variant']} | {checks} | {r.get('semantic_judge',{}).get('passed','未评分')} | {reason} |")
    lines += ['', '## 可解释范围', '',
              '- 仅证明固定数据、同一业务情境下六种反馈的表现，不能外推为广泛路由准确率。',
              '- 首轮广告建议已经是不增加预算，故预算项验证约束执行；不覆盖撤销一条原有加预算建议的冲突情况。',
              '- 范围规则要求 A 库存的用户可见字段有变化，其他建议整个对象相同；措辞变化不代表质量提高，因此另列语义判定。',
              '- 此任务已提供完整样例事实，要求不检索平台政策；不用于衡量 RAG 或工具循环质量。',
              '- 初始开发中出现传输中断、规划平台编号及引用错误，已保留本地诊断；不计作正式六条反馈的质量样本。',
              '- development-attempts 保留了契约修复前两条开发失败记录；正式保留测试在修复冻结后执行。',
              '- 范围保持检查针对 priorities 中的建议对象；摘要、风险和限制会重新综合，由语义评分与系统审核检查。',
              '- 执行失败计为端到端未通过，但不将其解释为已经生成报告的范围错误；请结合执行错误列与逐条原因阅读。',
              '- 未进行局部重算、长会话或跨会话记忆优化。', '',
              '- 生成与评分使用同一型号模型，可能共享偏差；LLM语义合格不等于独立人工真值。',
              '- 开发评分补齐审计和 Skill 上下文后重评一次，旧评分保存在 semantic_judge_history；保留测试没有重复评分。',
              '- 两组语义评分都可能通过，但基线仍改写非目标建议，因此“保持不变”以规则对象比较为准。', '',
              f"缺失记录：{', '.join(missing) if missing else '无'}", '']
    (folder/'summary.json').write_text(json.dumps(groups,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'README.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(groups,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
