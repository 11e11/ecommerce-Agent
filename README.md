# ecommerce-Agent

面向跨境电商运营的 Python Agent 运行时，包含多 Agent 协作、人工审批、审计、失败恢复，以及 Amazon、Amazon Ads 和 Shopify 连接器。

后端使用 FastAPI + Uvicorn，Agent 编排使用 LangGraph，任务与审计数据保存在 SQLite。

## 环境要求

- Python 3.10+
- 可选：OpenAI、Anthropic 或 DeepSeek API 凭据

## 安装

```bash
pip install -e ".[mcp,xlsx]"
```

## 模型配置

默认使用 OpenAI。使用 DeepSeek 时设置：

```bash
EAI_AGENT_PROVIDER=deepseek
DEEPSEEK_API_KEY=<YOUR_DEEPSEEK_API_KEY>
EAI_DEEPSEEK_MODEL=deepseek-flash
```

API Key 仅从环境变量读取，不写入 SQLite 或审计记录。

## 本地演示

```bash
opc-ecommerce demo-seed --db ./demo.sqlite
opc-ecommerce demo --db ./demo.sqlite --port 8788
```

然后访问 `http://127.0.0.1:8788/app`。

接口文档位于 `/docs`，机器可读的接口定义位于 `/openapi.json`。已有 `api`、`demo` 和 `pilot` 启动命令都使用同一套 FastAPI 服务。

```bash
opc-ecommerce api --db ./runtime.sqlite --host 127.0.0.1 --port 8787
```

同步业务与 Agent 执行使用线程池，实时事件流使用异步 SSE。SQLite、限流器和 SSE 连接计数目前按单进程运行，不应直接启动多个 Uvicorn worker 共享这一部署。

## 测试

```bash
pip install -r tests/requirements.txt
python -m pytest
```

## 主要目录

- `ecommerce_ai_skills/runtime/`：Agent 运行时、API、任务执行与连接器
- `ecommerce_ai_skills/runtime/web/`：管理界面
- `skills/`：运行时使用的领域技能定义
- `ontology/`：平台、实体、流程与约束定义
- `openapi/`：HTTP API 契约
- `integration/`：MCP 与外部集成
- `tests/`：自动化测试

原项目中的教程、三语电子书、Notebook、站点素材和生成产物仅保留在本地，不纳入此仓库。
