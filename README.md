# ecommerce-Agent

面向跨境电商运营的 Python Agent 运行时，包含多 Agent 协作、人工审批、审计、失败恢复，以及 Amazon、Amazon Ads 和 Shopify 连接器。

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
