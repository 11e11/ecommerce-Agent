# 进阶阶段：确定性选择执行与安全复用

## 问题、方案、取舍

用户在比较两个平台后要求“Amazon 不增加广告预算，只修改库存建议，Shopify 保持原样”。基础阶段保证修改范围，但仍重新生成 Shopify 分析后丢弃。进阶将已经明确的修改范围转换为 DAG 执行范围，不增加规划 Agent 或通用依赖引擎。

仍重新执行 Analyst。审计后由代码检查候选缓存，Amazon 必跑；Shopify 验证通过则直接把旧平台分析加入 findings，LangGraph 不再分发该 Specialist。跨平台协调、Manager、Reviewer 必跑。Manager 被明确告知哪些分析是复用，不能把旧计划说成本轮新执行。

## 如何检查复用

每次实际平台分析保存 specialist_dependencies：平台输入证据内容和时间、共享证据、原始任务、本平台预算约束、Skill 合同、Prompt、工具权限、模型配置、图/执行版本、本平台审计缺口的指纹，以及结果完整性摘要。

复用前要求父运行已通过审核、任务完成、指纹匹配、所有相关证据仍在既有有效期内。新版本、数据变更、约束变化、未知/不足结果、跨平台分析依赖、外部工具证据或任何旧工具调用无法重验时，保守重跑。没有元数据的旧运行也重跑。

partial 结果仅在本平台缺口未变化时可复用；partial 不变成 sufficient，原有缺口继续传给 Manager/Reviewer，执行门禁仍可能阻止外部动作。这是缓存既有分析，不是宣称问题已经解决。

当前不重验动态工具来源，因而该类任务常会回退全跑；这是明确边界。先保证输入报表型任务复用安全，再考虑更复杂的数据版本查询。

## 接口与可观测性

- follow-ups 增加 execution_mode：HTTP 默认 selective，full 保留完整重跑。Python 服务默认 full 兼容旧调用；需要选择性执行时显式传 selective。
- initial run 仍完整执行；旧版追问缺需求状态时保持完整重跑。
- execution_plan 保存每个平台 execute/reuse 与原因。task.reused 记录来源运行；复用任务没有 task.started，attempt_count=0，不冒充模型执行。
- Reviewer 如果要求重做复用平台，现有返修机制重新运行它；评测依据真实 task.started 计数，不只相信计划或 completed 状态。
- 幂等指纹包含 execution_mode；不在同一个请求编号下悄悄切换策略。

## 验证与评测

沿用六条用例及原有规则/语义评分协议，不新增业务类型。当前版本下重新生成一份真实首轮，三组共享：baseline 文本反馈完整重跑、stage_one 范围控制但全跑、stage_two 范围控制及选择执行。不得直接把旧版本的耗时当成当前对照。

仍禁用评测图的外部工具以隔离反馈处理；生产图权限不因此改变。收益只报告这类输入报表任务，不外推为 RAG 加速。

主验证：范围与预算结果不能变差；日志确认 A 实际运行、B 确实未调用；记录模型/工具调用、token 和耗时。并行角色少跑一个不保证同等比例降低延迟。

复现命令：

```powershell
ecom\Scripts\python.exe -X utf8 scripts/eval_revisions.py --db .tmp/selective-final.sqlite --output operations/revision-selective-eval --variants baseline stage_one stage_two --split development
ecom\Scripts\python.exe -X utf8 scripts/eval_revisions.py --db .tmp/selective-final.sqlite --output operations/revision-selective-eval --variants baseline stage_one stage_two --split held_out
ecom\Scripts\python.exe -X utf8 scripts/summarize_revision_eval.py --input operations/revision-selective-eval --variants baseline stage_one stage_two
```

功能、评测器与结果分阶段提交。仅实施进阶，没有增加跨会话长期记忆或自动偏好提炼。
