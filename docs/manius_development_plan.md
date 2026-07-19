# ManiusCode 开发计划（S0–S2 实现校准版）

本文档记录仓库当前已经落地的 S0–S2 功能，而不是早期的预期设计。当前可执行架构为：`manius-core` 是唯一的 Agent 执行进程，`manius` 是一次性 CLI 客户端，`manius-tui` 是常驻只读观测客户端。

```text
manius-core (daemon, TCP 127.0.0.1:7437)
  ├─ JSON-RPC 2.0 / NDJSON
  ├─ AgentRunner、事件持久化和 IPC 广播
  └─ runs/<run_id>/events.jsonl

manius (CLI)       ─┐
manius-tui (TUI)   ─┴─ SocketClient / JSON-RPC
```

## S0：双进程 IPC 基座（已完成）

### 目标

建立常驻 `manius-core` daemon 与 `manius` CLI 的 TCP JSON-RPC 通信链路，并以 `core.ping` 验证配置加载、请求分发和响应匹配。

### 已完成内容

- `ManiusConfig` 支持默认值、`~/.manius/config.toml`、项目 `.env` 和环境变量的分层加载。
- `SocketServer` 使用 `asyncio.start_server` 提供 TCP NDJSON 服务，支持 JSON-RPC 解析、参数校验、方法不存在和内部错误响应；同一连接上的请求可并发分发。
- `SocketClient` 以 UUID 请求 ID 匹配并发 RPC 响应，统一处理连接关闭和协议错误。
- daemon 注册 `core.ping`，返回版本号与运行时长；CLI 提供 `manius ping`。
- 项目入口已注册：`manius-core`、`manius` 和后续 S2 增加的 `manius-tui`。

### 验收

```bash
# 终端 A
uv run manius-core

# 终端 B
uv run manius ping
```

`tests/integration/test_ping.py` 覆盖真实 daemon 的 ping 链路以及同一长连接上的并发 ping 响应匹配。

## S1：Agent 执行内核与事件持久化（已完成）

### 目标

实现可由上层宿主复用的 Agent 执行链路：Plan → Act → Observe，具备结构化事件、工具调用、Claude 流式输出和每次运行的事件持久化。

### 已完成内容

- `ExecutionContext` 保存 `run_id`、目标、步骤、对话历史以及全局三态：`running`、`success`、`failed`；通过 `mark_success()`、`mark_failed()` 和 `is_done()` 统一控制状态流转。
- `AgentLoop` 在最大步数内驱动 Claude、工具调用和上下文追加；正常结束、异常和步数超限均写入上下文状态。
- `AgentRunner` 为每个运行创建 `runs/<run_id>/events.jsonl`，装配 `EventBus`、`EventWriter`、工具注册表、工具调用器、LLM Provider 与 AgentLoop，并返回 `RunSummary`。
- 事件模型统一位于 `core.bus.events`，包括运行、步骤、工具、LLM 请求、LLM token 与 LLM 响应事件；每个 Agent 事件携带 `kind="event"`、`run_id`、时间戳和步骤信息。
- 工具职责已拆分：`ToolRegistry` 仅负责注册和查询；`ToolInvoker` 负责执行、错误转换与 `tool_call_start/success/failed` 事件广播。当前实现的业务工具为 `ReadFileTool`。
- `AnthropicProvider` 使用 `AsyncAnthropic.messages.stream()` 输出 `LlmTokenEvent`，并在请求中设置 `cache_control={"type": "ephemeral"}`；最终结果以 `LlmResponseEvent` 广播。
- `StdoutPrinter` 和 `EventWriter` 保持为事件订阅者。当前 daemon 不订阅 `StdoutPrinter`，避免服务端与客户端重复打印。

### 当前边界

S1 形成的 Agent 内核仍保持独立、可注入和可测试；但当前 `manius run` 的实际入口已在 S2 改为远程调用 daemon，不再以 CLI 前台进程直接实例化 `AgentRunner`。

## S2：Daemon 执行、事件流与客户端观测（已完成）

### 目标

将 Agent 执行权收敛到 `manius-core`，让 CLI 与 TUI 通过同一套 SocketClient 和 JSON-RPC 协议启动、追踪和观测任务。

### Daemon 实现

- `CoreApp` 注册 `core.ping`、`agent.run`、`event.subscribe`、`event.unsubscribe` 和 `event.list`。
- `agent.run` 立即返回 `AgentRunResult(run_id)`，后台以 `asyncio.Task` 执行 `AgentRunner`；daemon 停止时会取消并等待仍在运行的任务。
- 任务参数错误也会分配 `run_id`，持久化并广播 `run_started` 与 `run_finished(status="failed", reason=...)`，使客户端可统一通过事件流获知终态。
- `IpcEventBroadcaster` 作为 `EventBus` 订阅者，以非阻塞 task 向匹配订阅推送事件；断开连接会自动清理订阅。
- 推送不再发送裸事件 JSON，而是标准 JSON-RPC 通知信封：

  ```json
  {"jsonrpc":"2.0","method":"event.push","params":{"kind":"event", "type":"..."}}
  ```

- `event.subscribe` 返回唯一 `sub_id`，支持 `run_id` 精确隔离或 `run_id=null` 全局观测，并支持 `topics` 的简单通配符过滤，例如 `step_*`、`tool_*`、`["*"]`。
- `event.unsubscribe(sub_id)` 可在不断开 TCP 的情况下主动释放订阅。
- `event.list(run_id)` 从 `runs/<run_id>/events.jsonl` 返回历史事件，用于快速任务和重连后的回放。

### CLI 实现

`manius run --goal "..."` 使用以下防丢事件时序：

1. 调用 `agent.run` 并立即获得 `run_id`。
2. 第一次调用 `event.list(run_id)`，渲染已持久化的历史事件。
3. 若已得到对应的 `RunFinishedEvent`，直接返回。
4. 调用 `event.subscribe(run_id, topics=["*"])` 建立精准实时订阅。
5. 第二次调用 `event.list(run_id)`，仅补齐内部完成状态，不重复打印。
6. 仅等待目标 `run_id` 的 `RunFinishedEvent`；在 `finally` 中主动退订并关闭连接。

连接、IPC 和响应校验失败会显示清晰错误信息并以非零退出码结束。

### TUI 实现

- `manius-tui` 是常驻只读观测端，使用 Textual `run_worker` 管理自动重连循环，并且每次重连都创建新的 `SocketClient`。
- 默认采用全局订阅：`run_id=null`、`topics=["*"]`；顶部状态栏显示 daemon 地址与连接状态。
- 使用 `VerticalScroll` 管理层级事件组件：运行、步骤、工具和结束事件是独立块；工具块会原地从 `running` 更新为 `done` 或 `failed`。
- LLM token 仅作为结果文本的传输数据：按短时间窗口批量写入同一个 `LlmStreamBlock`，流结束后升级为 Markdown 渲染，不输出原始 TokenEvent 日志行。
- TUI 包含 ManiusCode 块字符 Banner；按 `q` 退出时取消 Worker，由其清理逻辑退订并关闭连接。

### 验收

```bash
uv sync

# 终端 A：daemon
uv run manius-core

# 终端 B：常驻观测（先启动，查看后续产生的全局事件）
uv run manius-tui

# 终端 C：启动一次 Agent 运行
uv run manius run --goal "请帮我总结 README.md 文件内容"

# 自动化测试
uv run pytest tests/unit tests/integration -q
```

当前测试覆盖 S0 ping、S1 Agent/工具/LLM 流式与状态机，以及 S2 的异步 `agent.run`、事件信封、run_id/topic 订阅隔离、退订、历史回放、CLI 时序和 TUI 事件组件流。
