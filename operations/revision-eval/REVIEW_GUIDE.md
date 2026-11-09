# 基础阶段检查入口

## 建议查看顺序

1. [实现说明](../revision-stage-one.md)：只做基础阶段，Router 解释需求、Specialist 完整重跑、Manager 按范围合并、Reviewer 检查。
2. [六条反馈与固定数据](../../tests/revision-cases.yaml)：一个业务情境，2 条开发、4 条保留测试。
3. [评测报告](README.md)：规则计算与模型评分分列，包含实际调用、token 与耗时。
4. [首轮原始报告](initial.json)：真实 DeepSeek 生成，全部 12 次对照共享同一首轮；额外的第五条分析建议没有人为删除。
5. [完整角色输出样例](example-held-01-stage_one.json)：有效需求状态、Analyst、两个 Specialist、协调节点、Manager、Reviewer 及执行事件。
6. `held-01-baseline.json` 与 `held-01-stage_one.json`：直接对照原报告、修改结果及独立规则评分。

## 本次观察

- 12 次正式反馈运行全部完成，使用同一执行合同、首轮报告、数据集及模型配置。
- 基线的 6 次运行都出现了非目标建议的字段变化；基础方案 6 次均只改变 Amazon 库存建议。
- 两组预算字段检查与模型语义评分均为 6/6。改善的是严格的修改范围控制，不是把整体建议准确率提高到 100%。
- 调用中位数 10→11，输入 token 中位数 72,338.5→81,502.5，耗时中位数约 65.36→66.54 秒。增加了一次需求解释；本阶段未证明降本或提速。
- 全部运行仍执行两个平台 Specialist，没有实现进阶阶段的局部重算。

## 评测边界

- 为隔离反馈处理，专用协作图对两组都禁用外部工具，事实均在输入中；本报告不评价工具循环或检索质量。
- 模型语义评分会接受同义改写，不能替代“其他建议原样保留”的对象比较。规则检查必须独立于模型自己解释出的修改范围。
- 开发中曾把预算限制误当成允许改写广告建议；旧输出在 development-attempts 中。修复后才运行保留测试。
- 开发旧评分缺少审计与 Skill 上下文，误把系统时间计算和已注明假设的计算当成编造；旧评分保留在 semantic_judge_history。业务报告未因重评分重新生成，保留测试没有重评分。
- 六条用例只有措辞差异，不证明其他业务场景或长对话的泛化能力。生成与评分模型相同，仍有共享偏差。

## 运行与检查

评测器自动读取本地 `.env`，不会提交密钥。正常复现：

```powershell
ecom\Scripts\python.exe -X utf8 scripts/eval_revisions.py --split development
ecom\Scripts\python.exe -X utf8 scripts/eval_revisions.py --split held_out
ecom\Scripts\python.exe -X utf8 scripts/summarize_revision_eval.py
```

已存在的结果会跳过，避免覆盖正式记录。重新独立实验时指定新的 `--db` 和 `--output` 路径；不要与本次正式结果混合汇总。

相关回归覆盖需求状态、范围合并、预算限制、澄清、重复请求、租户隔离、完整 DAG、接口、Demo 兼容及有限格式修复。当前基础阶段停在这里，等待检查后再讨论进阶优化。
