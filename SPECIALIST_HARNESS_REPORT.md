# Specialist Harness 改造报告

## 已确认的处理方式

- Analyst 模型输出不合法时降级为 `unknown`，保留确定性体检并继续生成报告。
- 数据不足可以给出有限结论，但不能进入自动 Briefing 优先级与 Proposal 流程。
- 在 `feature/specialist-harness` 上继续已有修改，提交并推送；评测改动和个人面试材料保留本地。

## 改造后的流程

`目标 → 确定性 Router（Skill + effort_tier）→ Analyst 体检 → 平台 Specialist 并行 → 可选 Cross Controller → Manager → Reviewer → 执行资格检查`

Router 选择和图版本在运行创建时保存，重试继续使用原绑定。Analyst 先计算确定性证据体检，再进行一次结构化模型判断。体检包含数据时效、Skill 必需输入和跨平台指标可比性；模型不能抹去确定性缺口。

Agent 自主性集中在 Specialist：standard/deep 任务先制定计划，按步骤选择只读工具取证，再反思证据是否充分，最多重规划一次。simple 任务保留一次研究与回答。Cross Controller 使用一次受控知识检索和结构化汇总；Manager、Reviewer 无工具。已有 Reviewer 最多一次定向修订仍保留，最终报告以最后一次审核为准。

## 预算与工具治理

| 任务档位 | 计划步数上限 | 工具调用上限 | Ops 拉取上限 | 重规划上限 | 截止时间 |
| --- | ---: | ---: | ---: | ---: | ---: |
| simple | 0 | 4 | 0 | 0 | 120 秒 |
| standard | 5 | 6 | 2 | 1 | 240 秒 |
| deep | 8 | 8 | 2 | 1 | 360 秒 |

发布图权限可以进一步降低上限。工作记忆只属于当前任务尝试，最多保留 12 条已知事实，注入内容最多 2000 字符。未执行的计划步骤不能被反思输出宣称为已完成。

Specialist 可使用四个内部 MCP Ops 只读工具：Briefing、Metrics、Proposals、Evidence。平台由执行器绑定，模型不能跨平台改写参数；重复调用也消耗预算。MCP 子进程只接收白名单环境变量，网络与锁等待受剩余时间限制。工具不可用、超时或取证不足会保留缺口，并阻断自动下游。

实际工具输出会冻结为快照，包含来源、最多 400 字符摘录、获取时间及摘要。引用验证使用真实快照，忽略模型自行编造的研究记录。带时间筛选的 Briefing 工具只返回筛选后的序列，避免混入原时间窗的汇总数值。

## 产物与执行门槛

运行保存确定性体检、Analyst 判断、计划、反思、工具证据快照及步骤/调用/Token 用量事件。Manager 的 `evidence_approach` 必须逐平台说明取证方式与证据充分性。

Reviewer 批准与执行资格分开判断。即使审核批准，只要体检不是 `supported`，或任一平台证据不是 `sufficient`，`execution_gate.eligible` 仍为 false。报告可查看，自动下游无法使用。

## 升级与演示

已有租户需由管理员显式执行：

```powershell
$env:OPC_RUNTIME_API_KEY = '<管理员密钥>'
python -m ecommerce_ai_skills.cli graph-publish-default --db <数据库路径>
```

命令对当前执行契约幂等，发布新图并退役旧图，保留历史运行关联。旧契约排队运行应重新发起，Daily Ops 计划需绑定新图。本次未修改现有租户数据库的图发布状态。

```powershell
python scripts/demo_specialist_harness.py
```

演示使用固定数据与模拟 Provider/工具，无需外部模型或平台凭据，可展示体检、计划、调用、反思和取证思路。

## 验证记录

验证环境：`ragent` 的 Python 3.12 派生隔离环境 `.tmp/mcp-test-venv`，安装 MCP SDK、LangGraph 和 XLSX 测试依赖。

| 检查 | 结果 |
| --- | --- |
| Git 跟踪的运行时测试 + Harness 测试回归 | 312 通过、1 跳过、4 个 Windows 限制用例排除；新增边界测试夹具字段名笔误在随后复验中修正 |
| 修正后 Harness / 集成测试复验 | 16 全部通过，与上一行部分重叠 |
| 真实 MCP stdio Ops 往返 | 通过，验证子进程环境传递、四个工具发现、HTTP 读取和平台过滤 |
| 默认图发布 CLI | 通过，连续调用返回相同已发布版本 |
| 固定数据演示 | 通过，Reviewer 批准且充分证据的执行门槛开放 |
| `build_dist.py --check` | 通过，154 个构建文件一致 |
| `verify_all.py --i1` | 通过，0 问题 |

四个排除项分别涉及 Windows 的 POSIX 权限、两项符号链接权限和 SIGTERM 返回值。新增边界测试的修复仅修改测试中的 `max_tool_calls` 字段名，生产代码在最终回归之后未再改动。

全目录检查还发现仓库已有内容核查/Git 跟踪问题；未跟踪的旧 RC 清单需要另行发布更新，联网 wheel 构建受当前网络权限限制。这些检查不作为本次功能通过项。

本次没有验证真实模型效果或外部平台实时数据；没有 UI、跨天记忆或数据库迁移。跨天记忆仅在 runbook 中保留后续设计。
