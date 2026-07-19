# ManiusCode 开发计划（S0–S3 实现校准版）

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

## S3：全链路追踪与系统级统一时间线（已完成）

### 目标

解决S2阶段仅靠 `events.jsonl` 高层业务事件无法定位底层问题的调试盲区，在不侵入业务执行链路、不阻塞主事件循环的前提下，搭建覆盖**IPC层、内部事件层、LLM交互层**的全链路追踪体系。将系统所有边界I/O与核心内部事件按全局高精度时间戳汇入单一全局追踪文件，形成完整的系统行为时间线，支撑三类核心调试场景：

1. 验证客户端命令参数是否正确解析，定位错误发生在IPC层还是业务层
2. 核查发往LLM的完整请求参数，确认工具调用结果、对话上下文是否正确传入
3. 查看LLM原始响应详情，明确结束原因（主动结束/工具调用/长度截断）与Token用量细节

### 已实现内容

- 新增 `core.tracing.TracingProvider` 与 `TraceRecord`：采用有界 `asyncio.Queue` 接收五类追踪记录，批量文件 I/O 转交工作线程，确保业务协程只执行同步 `emit()` 与 `put_nowait()`；daemon 关闭时停止接收新记录、排空队列并关闭文件。
- 新增 `ManiusConfig.trace`：默认启用，默认文件为 `~/.manius/traces/daemon.jsonl`，并支持 `MANIUS_TRACE_ENABLED`、`MANIUS_TRACE_FILE`、`MANIUS_TRACE_MAX_QUEUE_SIZE`。追踪目录不可写时 daemon 会保留正常服务能力并记录告警。
- `SocketServer` 记录完整入站 JSON-RPC 帧、坏帧原文和所有响应；`IpcEventBroadcaster` 在写入前记录完整 `event.push` 信封。
- `EventBus` 以注入的全局追踪器记录每个业务事件；`AnthropicProvider` 记录完整请求参数和流结束后的完整最终响应，不单独记录 token 增量。
- `CoreApp` 在生命周期内创建、注入和优雅停止同一个追踪器；`AgentRunner` 将其传入每次运行的 EventBus 与默认 LLM Provider，保留既有 per-run `events.jsonl`。
- 已提供 `manius trace tail`、`manius trace filter --run-id ...`、`manius trace filter --direction ...` 和 `manius trace llm --run-id ...`，用于查看同一个全局 JSONL 文件。
- `tests/unit/test_tracing.py` 覆盖五种方向、坏帧、事件推送、LLM 请求/响应与队列 drain；配置覆盖位于 `test_config.py`，`tests/integration/test_tracing.py` 验证真实 daemon 的 `core.ping` 全局 IPC 追踪。

### 核心设计原则

1. **全局单文件**：所有追踪记录写入统一的全局文件，覆盖无run\_id的全局命令与跨run的时序问题
2. **非阻塞写入**：追踪写入完全不阻塞主asyncio事件循环，对业务性能零影响
3. **全量原始数据**：记录完整的原始请求/响应/事件 payload，不做裁剪，保留调试所需的全部信息
4. **与现有体系互补**：与per-run的 `events.jsonl` 分工明确，不重复、不冲突
5. **低侵入性**：通过埋点与订阅方式接入现有组件，无需修改核心业务逻辑

---

### Daemon 端核心实现

#### 1. 追踪数据模型：统一Trace记录规范

所有追踪记录采用统一结构，写入 `daemon.jsonl`，每行一条JSON，支持按时间排序、按字段过滤。

**统一字段定义**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timestamp` | string | 是 | ISO8601 高精度时间戳（毫秒级），全局排序依据 |
| `direction` | string | 是 | 数据流方向枚举，共5种，对应系统全量I/O与内部事件 |
| `run_id` | string/null | 是 | 关联的任务运行ID，全局命令/未解析出run\_id时为null |
| `trace_id` | string/null | 否 | 请求-响应关联标识（如JSON-RPC的id、LLM请求唯一标识） |
| `payload` | object | 是 | 完整原始数据，不同方向对应不同内容 |

**五个数据流方向定义**：

| 方向枚举 | 所属层级 | 含义 | payload内容 |
| --- | --- | --- | --- |
| `client_to_core` | IPC层 | 客户端发往守护进程的JSON-RPC请求 | 完整的请求帧：`id`、`method`、`params` |
| `core_to_client` | IPC层 | 守护进程发往客户端的响应与主动推送 | 完整的响应帧/通知帧：`id`、`result`/`error`、`method`、`params` |
| `core_event` | 事件层 | EventBus发布的所有业务事件 | 完整的事件对象，与`events.jsonl`中记录一致 |
| `core_to_llm` | LLM层 | 发往Anthropic API的完整请求 | 全量请求参数：`model`、`messages`、`tools`、`max_tokens`、`cache_control`等 |
| `llm_to_core` | LLM层 | Anthropic API返回的完整响应 | 全量响应体：`content`、`stop_reason`、`stop_sequence`、`usage`等 |

#### 2. 四个埋点：全覆盖五条数据流

在系统关键节点设置4个埋点，完整覆盖5个数据流方向，所有埋点统一调用追踪管理器的`emit()`接口。

##### 埋点1：SocketServer 请求入口（覆盖 client_to_core）

- **接入位置**：`SocketServer` 的请求处理流程中，NDJSON帧解析完成、方法分发之前
- **触发时机**：每收到一条完整的客户端请求帧（含解析失败的坏帧）
- **记录逻辑**：
  - 正常请求：记录完整JSON-RPC请求对象，`trace_id` 取请求的`id`字段
  - 解析失败的坏帧：记录原始文本与错误信息，用于排查协议兼容问题
  - 初始`run_id`为`null`，此时尚未解析业务参数

##### 埋点2：SocketServer 响应/推送出口（覆盖 core_to_client）

- **接入位置**：两处出口
  1. 方法处理器执行完成，向客户端返回JSON-RPC响应时
  2. `IpcEventBroadcaster` 向订阅端推送 `event.push` 通知时
- **触发时机**：所有向客户端写入NDJSON帧之前
- **记录逻辑**：
  - 响应帧：`trace_id` 取对应请求的`id`，关联请求与响应
  - 推送通知：从事件参数中提取`run_id`填充

##### 埋点3：EventBus 全局订阅（覆盖 core_event）

- **接入方式**：追踪管理器作为 `EventBus` 的全局订阅者，订阅 `topics=["*"]`
- **触发时机**：EventBus发布任意业务事件时
- **记录逻辑**：直接透传完整事件对象，从事件中提取`run_id`填充
- **说明**：与 `EventWriter` 写入 `events.jsonl` 的数据同源，用于和IPC、LLM数据对齐时间线

##### 埋点4：AnthropicProvider 请求前后（覆盖 core_to_llm + llm_to_core）

- **接入位置**：`AnthropicProvider` 的流式请求方法中
- **触发时机**：
  1. 发起API请求前，记录完整请求参数
  2. 流式接收结束、拿到完整响应后，记录完整响应体
- **记录逻辑**：
  - `core_to_llm`：记录全量请求参数，包含完整`messages`数组与工具定义，`run_id`从执行上下文中获取
  - `llm_to_core`：记录完整API响应，包含`stop_reason`、`usage`与所有content块
  - 流式token增量不单独记录，避免文件过度膨胀，token统计以最终响应的`usage`为准

#### 3. 非阻塞写入机制：队列 + 后台Drain任务

为避免文件I/O阻塞主事件循环，采用「内存队列缓冲 + 独立协程落盘」的异步写入方案。

**核心组件：TracingProvider（追踪管理器）**

- **内部结构**：
  - 持有 `asyncio.Queue` 作为内存缓冲队列，默认容量可配置
  - 持有后台 `drain_task` 协程，随CoreApp生命周期启停
  - 维护文件句柄，以追加模式打开追踪文件
- **emit() 方法**：
  - 同步非阻塞接口，内部调用 `queue.put_nowait()` 将记录入队后立即返回
  - 全程不执行文件I/O，不占用主事件循环时间
- **Drain 后台任务**：
  - 独立协程，循环从队列中取出追踪记录
  - 批量追加写入 `~/.manius/traces/daemon.jsonl` 文件
  - 队列为空时挂起，不消耗CPU资源
- **优雅关闭逻辑**：
  - `CoreApp` 停止时调用 `TracingProvider.stop()`
  - 停止接收新的`emit`请求，等待队列中所有剩余记录写入完成
  - 关闭文件句柄后再退出
  - 正常SIGTERM关闭无数据丢失；极端SIGKILL崩溃可能丢失队列中未写入数据，调试场景下可接受

#### 4. 存储方案：全局单一追踪文件

**文件路径**：`~/.manius/traces/daemon.jsonl`

- 采用JSON Lines格式，每行一条记录，按时间顺序追加写入
- 支持标准命令行工具（`jq`、`grep`、`tail`）直接过滤查看

**为什么不用per-run拆分文件**：

1. **run_id生成时序问题**：`agent.run` 请求到达IPC层时，尚未解析出`run_id`，第一条追踪记录必须早于run_id生成
2. **全局命令无归属**：`core.ping`、`event.subscribe`等全局守护进程命令，不属于任何一次任务运行
3. **跨run调试需求**：多任务并发、命令时序、连接复用等问题，需要全局统一时间线才能定位

**与现有 events.jsonl 的分工**：

| 文件 | 定位 | 适用场景 |
| --- | --- | --- |
| `runs/<run_id>/events.jsonl` | per-run业务深度档案 | 分析单次任务中Agent的执行步骤、工具调用、对话逻辑 |
| `~/.manius/traces/daemon.jsonl` | 系统级跨层时间线 | 调试命令流转、协议错误、LLM交互细节等底层问题 |

#### 5. 配置与生命周期集成

- **配置扩展**：`ManiusConfig` 新增trace配置项：是否启用追踪、追踪文件路径、队列最大长度，默认启用
- **CoreApp 集成**：
  1. 初始化时创建 `TracingProvider` 单例并注入到各组件
  2. 启动时启动drain后台任务
  3. 停止时调用`stop()`优雅关闭
- **组件注入**：`SocketServer`、`EventBus`、`AnthropicProvider` 均注入 `TracingProvider` 实例，完成埋点接入

---

### CLI 端配套工具（可选增强）

为提升调试效率，配套轻量命令行工具，支持快速过滤查看追踪数据。

#### `manius trace` 命令

- `manius trace tail`：实时追踪daemon.jsonl的新增记录，类似`tail -f`
- `manius trace filter --run-id <id>`：过滤指定run\_id的所有追踪记录
- `manius trace filter --direction <方向>`：过滤指定数据流方向的记录
- `manius trace llm --run-id <id>`：提取指定任务的完整LLM请求与响应，直接展示messages与stop\_reason

---

### 验收标准

#### 手动功能验收

```bash
# 1. 启动 daemon
uv run manius-core

# 2. 验证全局命令追踪
uv run manius ping
# 检查 ~/.manius/traces/daemon.jsonl，包含：
# - client_to_core 方向的 core.ping 请求
# - core_to_client 方向的 ping 响应
# 两条记录均无 run_id，时间戳递增

# 3. 验证全链路追踪
uv run manius run --goal "请读取 README.md 并总结"
# 检查 daemon.jsonl，按时间顺序包含以下记录：
# 1. client_to_core: agent.run 请求
# 2. core_event: run_started 事件
# 3. core_to_llm: 完整的 Claude API 请求（含messages与工具定义）
# 4. llm_to_core: 完整的 Claude API 响应（含stop_reason、usage）
# 5. core_event: 工具调用相关事件、step事件
# 6. core_event: run_finished 事件
# 7. core_to_client: agent.run 响应
# 8. core_to_client: event.push 推送通知
# 所有记录时间戳全局递增，run_id正确关联

# 4. 验证优雅关闭
# Ctrl+C 停止 daemon，检查队列中记录全部写入，无数据丢失

# 5. 验证调试能力
# 可通过 jq 过滤查看 LLM 的 stop_reason：
jq 'select(.direction == "llm_to_core") | .payload.stop_reason' ~/.manius/traces/daemon.jsonl
```

#### 测试覆盖

- **单元测试**：
  - `TracingProvider` 的emit入队、drain落盘、优雅关闭逻辑
  - 记录格式正确性验证
- **集成测试**：
  - 四个埋点的触发正确性验证
  - 五类数据流记录完整性验证
  - 多并发请求下的时序正确性
- **性能验证**：
  - 开启追踪后，Agent执行耗时与S2相比无显著增长
  - 主事件循环无阻塞卡顿

---

### 完成后能力边界

S3完成后，系统具备全链路可观测能力，可直接定位以下问题：

1. 客户端命令参数是否正确到达daemon，参数解析错误发生在IPC层还是业务层
2. 发往LLM的messages是否正确包含工具调用结果，上下文是否完整
3. LLM结束的真实原因（`end_turn`/`tool_use`/`max_tokens`），区分主动结束与被动截断
4. 事件推送是否丢失、延迟，IPC层与内部事件层的时序是否一致
5. 多客户端并发时的命令顺序与事件分发是否正确
