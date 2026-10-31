# FastAPI 后端

后端使用 FastAPI 显式路由和 Uvicorn ASGI 服务。`api.py` 保留服务对象装配、`create_app(runtime)` 工厂和原有启动函数；生产代码不再使用标准库 HTTPServer。

## 启动

```powershell
pip install -e ".[dev,xlsx]"
opc-ecommerce api --db ./runtime.sqlite --host 127.0.0.1 --port 8787
```

也可继续使用 `demo` 和 `pilot` 命令。演示建议使用新数据库路径，避免覆盖已有数据。`/docs` 是 FastAPI 自动接口文档，`/openapi.json` 是运行时生成的接口描述，`/app` 保留原管理界面。业务请求仍使用 `Authorization: Bearer ...`；幂等写请求保留 `Idempotency-Key`。

## 结构

- `runtime/api.py`：数据库、鉴权、Agent、工作流及其他服务的装配，FastAPI 工厂和启动入口。
- `runtime/http_routes.py`：显式 GET、POST、PATCH 路由，直接调用原有服务层。
- `runtime/http_context.py`：Bearer 鉴权依赖、JSON 字段校验、原始上传读取和统一响应。
- `runtime/http_schemas.py`：Agent 创建与反馈续办的 Pydantic 请求模型，拒绝未知字段。
- `runtime/http_app.py`：请求编号、限流、请求体大小上限、异常响应、静态资源和异步 SSE。
- `runtime/asgi_server.py`：Uvicorn 生命周期；预先绑定端口，使 pilot 能在端口不可用时、创建租户前失败。

同步数据库服务和 Agent 执行接口由 FastAPI 在线程池运行；SSE 使用异步生成器，数据库轮询也在线程池执行。保留游标回放、心跳、保留期重置、连接上限与断开后的计数释放。请求体按实际接收字节限制大小，包括没有 Content-Length 的分块请求。错误和访问日志不记录原始请求体或查询值，未预期异常返回通用 500 信息。

当前仍应按一个 Uvicorn worker 部署：SQLite、内存限流和 SSE 连接计数没有改造成跨进程共享状态。代理头默认不被 Uvicorn信任，生产环境需要按实际代理拓扑明确配置。

新增续办与轨迹接口照常使用：

```text
POST /v1/agent-runs/{runId}/follow-ups
GET  /v1/agent-runs/{runId}/trace
```

## 验证边界

`tests/test_fastapi.py` 通过 ASGI TestClient 验证实际路由、鉴权与租户隔离、参数错误、CSV/XLSX 上传、静态资源、限流、SSE 和两轮任务；另有绑定真实端口的 Uvicorn 启停测试，以及证明同步服务不会阻塞健康检查的并发测试。

历史测试对旧 `_Handler` 的依赖已移动到 `tests/legacy_http.py`，该文件只是迁移前的合同参照，不会被生产后端导入。新 HTTP 行为必须在 `test_fastapi.py` 验证，不能仅凭旧 Handler 测试认定 ASGI 功能正常。

Windows 的 `Popen.send_signal(SIGTERM)` 实际调用 TerminateProcess，不能验证优雅退出，因此该 POSIX 信号测试只在支持的平台运行；Windows 通过真实 HTTP + stop_event 的 Uvicorn 测试验证关闭，原有 pilot 生命周期的服务层测试继续保留。

```powershell
python -m pytest tests/test_fastapi.py tests/test_continuations.py tests/test_agent_graphs.py tests/test_harness_integration.py tests/test_pilot.py tests/test_runtime.py
```
