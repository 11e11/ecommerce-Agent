# Router 前置与受控 Agent 工作流改进报告

## 改进后的流程

确定性 Router 按任务选择一个或多个 Skill，并按输入平台过滤。路由结果随运行保存，重试复用同一结果。无匹配、知识约束覆盖不足或平台没有适用 Skill 时，创建运行返回明确原因。

执行顺序为：Evidence Analyst 与各平台 Specialist 并行 → 多平台时 CrossPlatformController → Manager → Reviewer。

Reviewer 可指定一次定向修订：退回对应平台 Specialist、CrossPlatformController 或 Manager，重跑受影响的下游角色并再次审核。第二次仍未批准时，结果不能进入下游流程。每次任务产物保留历史版本。

## Agent 能力

- Evidence Analyst、Specialist、CrossPlatformController 可按需调用 MCP 只读知识工具；默认每个任务最多调用六次。
- 工具白名单、调用次数、结果长度和调用审计由确定性代码控制。
- Manager 负责综合优先级；Reviewer 依据固定证据和产物独立校验，两者均无工具。
- 当前工具读取项目知识包，不查询实时电商平台，也不执行外部写入。

简历可表述为：通过 MCP 为项目内 Agent 提供知识检索能力，结合确定性路由、并行平台分析和一次定向修订，构建受控多 Agent 工作流。

## 验证与实际限制

本次一并提交 RAG 实现：Milvus 内部 BM25 与稠密向量检索、RRF 融合，以及本地文件检索回退。修复 AnnSearchRequest 参数兼容问题，六项 RAG 功能单元测试通过；这些测试使用模拟客户端，不代表真实 Milvus 服务验证。评测脚本、评测数据和评测结果不提交。

已验证单平台、多平台、多 Skill 平台过滤、无匹配与低覆盖阻断、工具越权和预算拒绝、三类修订目标、最终 Reviewer 审批及重试历史。

最终复测：`tests/test_agent_graphs.py` 与 `tests/test_tool_phase.py` 共 24 项全部通过，包括真实 MCP 往返测试。此前运行时、Daily Ops 和 Demo 测试组也已通过。

MCP 往返测试实际启动本地服务，通过 stdio 初始化会话，调用 `opc.search_knowledge`，检查返回结果并关闭连接。它验证工具通信，不验证真实模型的分析质量或外部平台连接。

测试使用基于 ragent Python 3.12 创建的 `.tmp/mcp-test-venv`。ragent 已安装 MCP SDK 1.30.0；原有 LangChain 版本约束与项目 LangGraph 依赖冲突，因此项目测试依赖放在隔离环境。被升级的 Conda 原有依赖已恢复。

修复了 Windows 默认编码导致生成 Markdown 无法被 MCP 服务按 UTF-8 读取的问题，构建脚本现在明确写入 UTF-8。

全量测试仍存在原有分发文件 Git 跟踪和事实审查统计问题；没有将这些问题计为本次验证通过。

## 启用方式

新建租户使用更新后的默认图定义。现有租户需要管理员基于新执行合约创建并发布图版本，再绑定新的运行或计划。旧版本及运行历史保留；本次没有修改已有租户数据库或声称已经上线。
