# 基础阶段：用户修改建议

目标：用户要求“Amazon 不增加广告预算，只修改库存建议，Shopify 保留”后，可靠更新报告。两个平台仍完整执行，不实现局部重算或跨会话长期记忆。

## 使用方式

- `POST /v1/agent-runs` 默认 `revision_support=true`，首轮报告增加 category、budget_change、advice_id 和 requirement_version。内部 Python 调用需显式 `revision_support=True`。
- `POST /v1/agent-runs/{id}/follow-ups` 仍接收 feedback。新版首轮报告会先调用 requirement_router，生成并校验需求更新；不明确时返回 `status=needs_clarification` 和 question，不创建运行。
- 创建的子运行依旧由 `/execute` 完整执行。Manager 生成候选结果后，代码只替换指定平台、指定类别的既有建议，其余建议逐字段复制。新报告包含 revision_changes。
- Reviewer 同时接收有效需求、上一版报告和 revision_rule_checks；规则不通过不能被模型批准。正文与字段是否一致仍由模型审核。
- 旧版报告保留原 follow-ups 行为。`revision_support=false` 可使用原有行为。

## 取舍与边界

- 需求状态存放在现有不可变运行的 continuation_context 记录中，不新增数据库表。首轮上下文没有父运行，子运行仍检查父运行已完成、审核通过且属于当前租户。
- 单次反馈只修改一个平台的既有建议，不新增或删除建议。跨平台合并建议无法安全局部替换时拒绝，并要求明确范围。
- 第一阶段只接受反馈，不同时更换证据或目标；数据更新属于后续范围。
- 如果要求保留广告建议，但该建议原来包含增加预算，与新约束冲突，先请求澄清。
- 结构化预算字段不等价于正文语义正确，规则评测与语义评测分别报告。
- Skill 路由沿用原始任务为其他平台分配能力，根据结构化修改类别为目标平台重新匹配规则 Skill；无法支持的目标能力明确报错。
- 原始数据库中的证据、工具快照仍是引用依据。历史报告只提供修订基线，不作为新事实来源。

## 阶段交付

先提交功能及回归验证，再提交六条用例和可复现评测器，最后提交真实模型运行结果。评测初始报告由真实模型生成一次固定使用；不得用固定 Demo 响应替代。未经过进阶优化，调用次数不预期下降。
