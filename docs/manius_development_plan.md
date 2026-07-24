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

- 新增 `core.tracing.TracingProvider` 与 `TraceRecord`：记录以 `ts`、`layer`、`direction`、`kind`、`run_id`、`step`、`client_id`、`trace_id` 和开放的 `data` 组成；采用有界 `asyncio.Queue` 接收五类追踪记录，批量文件 I/O 转交工作线程，确保业务协程只执行同步 `emit()` 与 `put_nowait()`；daemon 关闭时停止接收新记录、排空队列并关闭文件。
- 新增 `ManiusConfig.trace`：默认启用，默认文件为 `~/.manius/traces/daemon.jsonl`，并支持 `MANIUS_TRACE_ENABLED`、`MANIUS_TRACE_FILE`、`MANIUS_TRACE_MAX_QUEUE_SIZE`。追踪目录不可写时 daemon 会保留正常服务能力并记录告警。
- `SocketServer` 记录完整入站 JSON-RPC 帧、坏帧原文和所有响应；`IpcEventBroadcaster` 在写入前记录完整 `event.push` 信封。
- `EventBus` 以注入的全局追踪器记录每个业务事件；`AnthropicProvider` 记录完整请求参数和流结束后的完整最终响应，不单独记录 token 增量。
- `CoreApp` 在生命周期内创建、注入和优雅停止同一个追踪器；`AgentRunner` 将其传入每次运行的 EventBus 与默认 LLM Provider，保留既有 per-run `events.jsonl`。
- 已提供 `manius trace tail`、`manius trace filter --run-id ...`、`manius trace filter --direction ...` 和 `manius trace llm --run-id ...`，用于查看同一个全局 JSONL 文件。
- `tests/unit/test_tracing.py` 覆盖五种方向、坏帧、事件推送、LLM 请求/响应与队列 drain；配置覆盖位于 `test_config.py`，`tests/integration/test_tracing.py` 验证真实 daemon 的 `core.ping` 全局 IPC 追踪。

### 核心设计原则

1. **全局单文件**：所有追踪记录写入统一的全局文件，覆盖无run\_id的全局命令与跨run的时序问题
2. **非阻塞写入**：追踪写入完全不阻塞主asyncio事件循环，对业务性能零影响
3. **全量原始数据**：记录完整的原始请求/响应/事件 `data`，不做裁剪，保留调试所需的全部信息
4. **与现有体系互补**：与per-run的 `events.jsonl` 分工明确，不重复、不冲突
5. **低侵入性**：通过埋点与订阅方式接入现有组件，无需修改核心业务逻辑

---

### Daemon 端核心实现

#### 1. 追踪数据模型：统一Trace记录规范

所有追踪记录采用统一结构，写入 `daemon.jsonl`，每行一条JSON，支持按时间排序、按字段过滤。

**统一字段定义**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts` | string | 是 | ISO8601 毫秒级时间戳，全局排序依据 |
| `direction` | string | 是 | 数据流向：`CLIENT->CORE`、`CORE>CLIENT`、`CORE`、`CORE>LLM`、`LLM>CORE` |
| `layer` | string | 是 | 子系统：`ipc`、`event` 或 `llm` |
| `kind` | string | 是 | 更细粒度分类，例如 `request`、`response`、`push` 或业务事件类型 |
| `run_id` | string/null | 是 | 关联任务运行 ID；全局命令或尚未生成任务 ID 时为 `null` |
| `step` | int/null | 是 | Agent 步骤；不属于任务步骤的记录为 `null` |
| `client_id` | string/null | 是 | IPC 客户端 TCP 对端地址；非 IPC 记录为 `null` |
| `trace_id` | string/null | 否 | 请求-响应或 LLM 往返关联标识 |
| `data` | object | 是 | 开放载荷；IPC 放原始帧，事件层放完整事件，LLM 层放完整报文及统计摘要 |

**五个数据流方向定义**：

| direction | layer | kind | data内容 |
| --- | --- | --- | --- |
| `CLIENT->CORE` | `ipc` | `request`、`parse_error`、`invalid_request` | 原始 JSON-RPC 请求帧或坏帧文本与错误 |
| `CORE>CLIENT` | `ipc` | `response` | 完整 JSON-RPC 响应帧 |
| `CORE>CLIENT` | `ipc` | `push` | 完整 `event.push` 通知信封 |
| `CORE` | `event` | 业务事件 `type` | 完整 EventBus 事件，与 `events.jsonl` 一致 |
| `CORE>LLM` | `llm` | `request` | 完整请求、`message_count` 与 `tool_count` |
| `LLM>CORE` | `llm` | `response` | 完整最终响应、`usage` 与 content 块数量 |

#### 2. 四个埋点：全覆盖五条数据流

在系统关键节点设置4个埋点，完整覆盖5个数据流方向，所有埋点统一调用追踪管理器的`emit()`接口。

##### 埋点1：SocketServer 请求入口（`CLIENT->CORE` / `ipc`）

- **接入位置**：`SocketServer` 的请求处理流程中，NDJSON帧解析完成、方法分发之前
- **触发时机**：每收到一条完整的客户端请求帧（含解析失败的坏帧）
- **记录逻辑**：
  - 正常请求：`kind="request"`，记录完整JSON-RPC请求对象，`trace_id` 取请求的`id`字段
  - 解析失败的坏帧：`kind="parse_error"`，记录原始文本与错误信息
  - 初始`run_id`为`null`，此时尚未解析业务参数
  - `client_id`使用 TCP 对端地址

##### 埋点2：SocketServer 响应/推送出口（`CORE>CLIENT` / `ipc`）

- **接入位置**：两处出口
  1. 方法处理器执行完成，向客户端返回JSON-RPC响应时
  2. `IpcEventBroadcaster` 向订阅端推送 `event.push` 通知时
- **触发时机**：所有向客户端写入NDJSON帧之前
- **记录逻辑**：
  - 响应帧：`kind="response"`，`trace_id` 取对应请求的`id`，关联请求与响应
  - 推送通知：`kind="push"`，从事件参数中提取`run_id`和`step`填充

##### 埋点3：EventBus 全局订阅（`CORE` / `event`）

- **接入方式**：追踪管理器作为 `EventBus` 的全局订阅者，订阅 `topics=["*"]`
- **触发时机**：EventBus发布任意业务事件时
- **记录逻辑**：直接透传完整事件对象，事件 `type` 写入 `kind`，并从事件中提取`run_id`和`step`
- **说明**：与 `EventWriter` 写入 `events.jsonl` 的数据同源，用于和IPC、LLM数据对齐时间线

##### 埋点4：AnthropicProvider 请求前后（`CORE>LLM` / `LLM>CORE` / `llm`）

- **接入位置**：`AnthropicProvider` 的流式请求方法中
- **触发时机**：
  1. 发起API请求前，记录完整请求参数
  2. 流式接收结束、拿到完整响应后，记录完整响应体
- **记录逻辑**：
  - `CORE>LLM`：`kind="request"`，记录全量请求、`message_count`和`tool_count`
  - `LLM>CORE`：`kind="response"`，记录完整API响应、`usage`与所有content块
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
# - CLIENT->CORE / ipc / request 的 core.ping 请求
# - CORE>CLIENT / ipc / response 的 ping 响应
# 两条记录均无 run_id，ts 递增且 trace_id 一致

# 3. 验证全链路追踪
uv run manius run --goal "请读取 README.md 并总结"
# 检查 daemon.jsonl，按时间顺序包含以下记录：
# 1. CLIENT->CORE / ipc / request: agent.run 请求
# 2. CORE / event / run_started: 任务启动事件
# 3. CORE>LLM / llm / request: 完整 Claude 请求及消息统计
# 4. LLM>CORE / llm / response: 完整 Claude 响应及 usage
# 5. CORE / event: 工具调用和步骤事件
# 6. CORE / event / run_finished: 任务结束事件
# 7. CORE>CLIENT / ipc / response: agent.run 响应
# 8. CORE>CLIENT / ipc / push: event.push 通知
# 所有记录 ts 全局递增，run_id、step 和 trace_id 正确关联

# 4. 验证优雅关闭
# Ctrl+C 停止 daemon，检查队列中记录全部写入，无数据丢失

# 5. 验证调试能力
# 可通过 jq 过滤查看 LLM 的 stop_reason：
jq 'select(.direction == "LLM>CORE" and .layer == "llm") | .data.response.stop_reason' ~/.manius/traces/daemon.jsonl
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

## s4: Agent的自主规划

## 一、阶段核心目标

将原 IPC 层的**外部人工操控任务系统**，内化为 Agent 的**私有认知规划工具**，实现能力跃迁：用户仅需输入单一高层目标，Agent 自主完成「任务拆解 → 依赖编排 → 逐任务执行 → 结果交付」全流程，无需人工分步触发指令。
全程复用现有 AgentLoop 执行内核与事件体系，通过扩展工具集的方式赋予 LLM 自主规划能力，同时保留 CLI/TUI 的全链路可观测性。

## 二、核心架构改造要求

1. **AgentRunner 集成 TaskManager**
   - 在 `AgentRunner.run_and_capture()` 中为每个 run 独立初始化 `TaskManager` 实例，任务存储路径固定为 `runs/<run_id>/.tasks`，实现单 run 数据隔离，支持历史规划回溯。
   - 将 `TaskManager` 实例注入工具构建流程，4 个任务工具共享同一实例，确保任务读写状态一致。
   - 不改动 S2/S3 已有的 run\_id 生成、后台任务调度、事件广播机制，保持架构向下兼容。
2. **工具注册表扩容**
   - 工具总数从原有基础工具扩展为 8 个：4 个任务规划工具 + 4 个执行类工具。
   - 所有工具统一接入 `ToolRegistry`，复用原有 `ToolInvoker` 调用逻辑与工具事件广播机制，无需改造执行内核。

## 三、核心模块实现规范：TaskManager

### 定位

纯同步文件 CRUD 层，**无异步方法、无 EventBus 依赖、无内置状态机**，仅负责任务数据持久化与依赖自动级联，所有规划决策完全交由 LLM 处理。

### 存储设计

- 每个任务对应一个独立 JSON 文件，命名格式 `task_<整数ID>.json`，存储于对应 run 的`.tasks`目录下。
- 任务 ID 使用自增整数，禁止使用 UUID，降低 LLM 记忆成本与调用错误率。
- 任务固定字段：`id`(int)、`subject`(str)、`description`(str)、`status`(三态枚举：pending/in\_progress/completed)、`blocked_by`(list\[int\])、`created_at`(ISO 时间戳)、`updated_at`(ISO 时间戳)。
- 仅保留基础三态，不实现 pause、retry、cancel 等流程状态，相关逻辑由 LLM 通过工具调用自主控制。

### 核心方法

1. `create(subject, description="", blocked_by=None)`
生成自增 ID，写入任务文件，返回任务对象；支持创建时直接指定依赖任务 ID。
2. `update(task_id, status=None, add_blocked_by=None, remove_blocked_by=None)`
读取对应任务文件，更新指定字段；若将状态更新为`completed`，自动触发依赖清理逻辑。
3. `clear_dependency(completed_id)`
扫描`.tasks`目录下所有任务文件，移除所有任务`blocked_by`字段中的已完成任务 ID，自动更新文件时间戳，实现依赖自动解锁，无需 LLM 手动维护。
4. `list()`
返回全部任务列表，输出为 LLM 友好的紧凑文本格式：用`[]`标记 pending、`[>]`标记 in\_progress、`[x]`标记 completed，同步标注`blocked_by`依赖，便于 LLM 快速判断可执行任务。
5. `get(task_id)`
读取单个任务的完整 JSON 数据，返回详情。

### 约束

- 所有方法均为同步实现，直接操作本地文件，不引入任何异步逻辑。
- 轻量化设计：任务量级为个位数到十几，文件 IO 开销可忽略，不引入数据库，便于直接通过命令行调试查看。

## 四、工具集实现规范

共 8 个工具，全部遵循原有工具抽象规范，支持自动生成 schema、被 ToolInvoker 调用、触发标准工具调用事件。

### 1. 任务规划工具（4 个）

- `TaskCreateTool`：封装`TaskManager.create`，入参为任务主题、描述、依赖 ID 列表，返回创建结果。
- `TaskUpdateTool`：封装`TaskManager.update`，入参为任务 ID、目标状态、依赖调整项，返回更新后任务信息。
- `TaskListTool`：封装`TaskManager.list`，无必选入参，返回格式化的全量任务状态摘要。
- `TaskGetTool`：封装`TaskManager.get`，入参为任务 ID，返回单个任务完整详情。

### 2. 执行类工具（4 个）

- `ReadFileTool`：读取本地文件内容，设置 512KB 大小上限，超出自动截断。
- `WriteFileTool`：写入文件内容，自动创建父目录，增加路径穿越防护。
- `ListDirTool`：列出目录结构，支持控制递归深度。
- `BashTool`：执行 Shell 命令，合并标准输出与错误输出，设置 64KB 输出大小上限。

## 五、Agent 执行链路适配

1. **完全复用 AgentLoop 核心逻辑**
无需修改 S1 已实现的`plan → observe → act`循环框架与状态机，仅需在调用 LLM 时，将全部 8 个工具的 schema 传入`tool_schemas`，由 LLM 根据系统提示自主决策是否拆解任务、何时执行工具。
2. **典型自主规划执行流**
   - 步骤 1：LLM 识别目标复杂度，调用`task_create`批量创建带依赖关系的任务清单。
   - 步骤 2：LLM 调用`task_update`标记任务为进行中，调用对应执行工具完成任务内容。
   - 步骤 3：LLM 标记任务为已完成，TaskManager 自动解锁下游依赖任务。
   - 循环往复，直至所有任务完成，LLM 输出最终总结结果。
3. **事件体系兼容**
所有任务操作均通过原有`tool_call_start/tool_call_finished`事件对外广播，**不新增独立的任务领域事件**。CLI 与 TUI 无需改造，可直接通过事件流观测规划全过程。

## 其他效果优化

### TUI改版：单列终端滚动流
整个界面为一个`verticalScroll`容器，事件进来时动态追加到wiget,始终自动滚动到底部。

### LLM流式输出的原地积累

每到一个token,self._text追加，然后用新字符串刷新widget显示。流结束时（收到非token事件)，finalize_markdown()把累积的文本整体渲染成Markdown,代码块、列表、粗体都正确显示。

**为什么不每个token追加一个widget?**
如果每个token都mount一个新widget,一次LLM回复可能产生几百个widget对象，Textual的布局引擎需要反复计算整个wⅵdget树的高度和位置，帧率会显著下降，长输出时肉眼可见地卡顿。update()在原地替换内容，布局引擎只需要重绘这一个widget,代价小得多。current1lm是当前"活跃"的LLMStreamBlock引用。当收到任何非token事件时，break1lm()调用finalize markdown()并把引用置为None:下一个token到来时会新建一个LLMStreamBlock,形成一个新的文字块。LLM在不同step里的思考内容在视觉上是分隔的，不会混在一起。

### 工具调用块的折叠展开

工具调用频繁，但只用关心其是否成功。全部展开会淹没llm思考与输出结果，全部折叠又无法了解调用细节。解决方法是：默认折叠，点击展开。
.detail这个子widget默认是display:none-存在于DOM里，但不占空间、不显示。给父widget加上expanded类后，CSS规则ToolCallBlock.expanded>.detail{display:block;}立即生效，detail出现。折叠/展开是纯CSS切换，不需要mount/remove widget,.也不需要重新布局整棵树，只是修改显示属性。点击行为很流畅。工具出错时，摘要行的颜色变红，折叠状态下就能看到出了什么问题一不需要展开细节。工具输出通过ToolCallFinishedEvent.output字段传递给TUI。s3在这个event上新增了output:str字段，invoke_tool()在publish之前把result.content塞进去，这样TUI能直接从事件里拿到工具输出，不需要回查events.jsonl。

## 六、设计原则与验收标准

### 设计约束

- **向后兼容**：原有单步执行模式完全保留，简单目标 LLM 可直接执行，无需强制拆解任务。
- **LLM 友好**：整数 ID、紧凑状态格式、自动依赖清理，最大化降低 LLM 调用错误率与认知负担。
- **数据隔离**：不同 run 的任务数据完全隔离，任务文件随 run 持久化，可回溯任意历史 run 的规划过程。
- **极简内核**：TaskManager 仅做数据持久化，所有规划决策、流程控制全部下沉给 LLM，避免重复造轮子。

### 验收用例

执行命令：

```
uv run manius run --goal "分析当前项目代码结构并生成结构说明文件"
```

验收标准：

1. Agent 自主拆解任务、设置依赖，无需人工干预即可完成全流程。
2. 任务完成时自动解锁下游依赖，状态流转正确。
3. TUI/CLI 可通过事件流观测到完整的任务创建、执行、完成过程。
4. run 目录下生成`.tasks`文件夹，包含完整的任务 JSON 文件，可直接查看规划历史。

## S4-Advance：五层闭环自主规划（开发提示词）

### 阶段定位

在 S3 的 daemon、IPC、事件广播、TUI 观测和全链路 Trace 基础上，将当前 S4 的“LLM 直接调用 `task_*` 工具管理任务”升级为五层闭环自主规划架构。目标是让用户只提供高层目标，系统自动完成：

```text
目标 → 规划 → 审计 → 调度/执行 → 验证 → 反思/修复 → 计划修订 → 总结/记忆沉淀
```

五层是职责边界，不是五个独立进程或五套 LLM 客户端。Planner、步骤动作推理和 Resolver 可复用同一个 `AnthropicProvider`；Auditor、Scheduler、Verifier、状态机和持久化必须由确定性代码实现。

所有新增核心代码必须位于 `src/manius_code/core/`，顶层包仍只保留 `cli`、`core`、`tui`。保留 `agent.run`、现有 JSON-RPC/SocketClient、EventBus、IpcEventBroadcaster、Trace、CLI 和 TUI 的公开接口与行为。

### 设计原则与强制约束

1. **LLM 只做推理**：LLM 只能提交经 Pydantic 校验的计划提案、动作提案和修复提案；不得直接拥有步骤状态控制权或执行副作用。
2. **机器规则兜底**：只有 Supervisor、Auditor、Executor 与 Verifier 能改变权威步骤状态；只有所有必需步骤验收通过后，才能发布 `RunFinishedEvent(status="success")`。
3. **禁止自然语言关键词补丁**：不得新增类似 `_requires_file_output(goal)` 的目标文本特判。交付物、验收条件、允许工具和风险限制必须来自结构化 `PlanStep`。
4. **复用而非复制**：复用 `AgentRunner` 的 run 生命周期、`ExecutionContext` 的短期上下文、`ToolRegistry`/`ToolInvoker` 的唯一工具执行入口、EventBus/IPC/Trace 观测链路，以及 `TaskManager` 的 run 隔离 JSON 持久化和依赖处理能力。
5. **无隐式副作用**：文件写入、命令执行等动作必须先经 Auditor 审计，再通过 `ToolInvoker.invoke()` 执行。Planner、Resolver、Store 不得直接调用工具。
6. **计划版本不可覆盖**：每次重规划产生新版本；不得创建与现有 `.tasks` 平行且不一致的第二套任务状态目录。
7. **复杂度自适应**：简单目标可生成单步骤计划，复杂目标才生成 DAG。是否规划由确定性复杂度策略决定，不能仅依赖 LLM 是否“自觉”调用 `task_create`。

### 五层职责

| 层 | 输入 | 输出 | 责任边界 |
| --- | --- | --- | --- |
| Planner | `GoalSpec`、相关记忆、当前计划摘要 | `PlanProposal` | 分层拆解任务、生成 DAG、声明交付物与验收条件；不执行工具、不写状态。 |
| Auditor | `PlanProposal`、`PlanPatch`、`ActionProposal`、策略 | `AuditResult` | 校验 DAG、工具权限、路径、预算、风险和验收条件；拒绝非法提案。 |
| Executor | 已批准计划、ready 步骤 | `StepResult`、`Artifact` | 调度步骤、请求步骤范围内的动作提案、复用 ToolInvoker 执行、记录事实。 |
| Resolver | 失败 `StepResult`、步骤上下文、记忆 | `ResolverDecision` | 提议 `retry`、`revise_step`、`replan` 或 `abort`；修订必须再次审计。 |
| Memory | 已验证 run 摘要、计划、失败和产物 | `MemoryContext`、`MemoryRecord` | 在规划/修复前提供压缩经验，结束后仅沉淀已验证结论。 |

`Verifier` 是 Executor 的必需子职责：它验证文件、测试、命令和显式产物；没有验收证据，任何步骤都不能进入成功状态。

### 建议模块结构

```text
src/manius_code/core/
  agent/
    runner.py              # 保留：run 生命周期与依赖装配
    context.py             # 保留：单 run 短期消息上下文
    loop.py                # 逐步迁移为兼容层，不再承担全局状态机

  autonomy/
    contracts.py           # GoalSpec、Plan、PlanStep、ActionProposal 等 Pydantic 模型
    supervisor.py          # 五层闭环的唯一状态机与结束判定
    planner.py             # Planner 的结构化 LLM 调用适配
    auditor.py             # 计划与动作的确定性规则审计
    scheduler.py           # 基于依赖与状态选择 ready 步骤
    executor.py            # 单步骤动作循环，复用 ToolInvoker
    verifier.py            # 文件、命令、测试和声明产物的验收
    resolver.py            # 失败反思、有限重试、计划修订提案
    store.py               # PlanStore：计划版本和步骤状态持久化门面
    policy.py              # 工具权限、预算、风险和重试策略

  memory/
    contracts.py           # MemoryRecord、MemoryContext
    working.py             # ExecutionContext 的压缩摘要策略
    episodic.py            # run 历史的结构化经验读写
    project.py             # workspace 隔离的稳定项目知识
    retrieval.py           # 检索、排序、截断记忆
```

不要复制 `TaskManager`。通过 `PlanStore` 门面逐步扩展并复用其文件隔离、JSON 读写、自增 ID 与依赖处理能力。保留现有 `task_*` 工具用于兼容和查看历史 `.tasks`；新 Supervisor 路径不得依赖 LLM 直接调用这些工具改变权威状态。

### 结构化契约和状态机

所有契约放入 `core/autonomy/contracts.py`，并在各边界使用 `model_validate`。最低模型集合：

```text
GoalSpec
  goal, workspace, required_artifacts, constraints

Plan
  plan_id, version, goal, steps, created_at

PlanStep
  id, title, description, dependencies, status,
  acceptance_criteria, allowed_tools, artifacts,
  attempt_count, last_error

ActionProposal
  step_id, tool_name, arguments, rationale

StepResult
  step_id, attempt, observations, artifacts, verification, error

ResolverDecision
  action: retry | revise_step | replan | abort,
  reason, patch, retry_arguments
```

步骤状态至少为：

```text
pending → ready → running → verifying → succeeded
                                  ├→ retryable
                                  ├→ replan_required
                                  └→ failed
```

状态转移由 `AutonomousSupervisor` 和 `PlanStore` 强制校验。Planner 和 Resolver 仅能提议；Verifier 产出验收证据后，Supervisor 才能写入 `succeeded`。

### 闭环执行要求

```text
Memory.retrieve
  → Planner.propose
  → Auditor.approve_plan
  → PlanStore.persist(version)
  → Scheduler.next_ready_step
  → Executor.execute_step
  → Verifier.verify
  → 成功：更新步骤并继续调度
  → 失败：Resolver.decide → Auditor.approve_patch → PlanStore.persist(next version)
  → 全部必需步骤验证成功：Memory.record → RunFinished(success)
```

#### Planner

- 输出 `PlanProposal`，不得调用 `task_create` 修改状态。
- 每个步骤必须有输入、依赖、允许工具、显式产物和可机器验证的 `acceptance_criteria`。
- 复用 `AnthropicProvider` 的流式调用、事件发布和 Trace；增加受限结构化推理接口或 schema，不创建平行 LLM 客户端。

#### Auditor

- 审计计划：DAG 无环、步骤 ID 唯一、依赖存在且无自依赖、验收条件完整、允许工具已注册。
- 审计动作：动作必须属于当前 ready 步骤的允许工具，参数满足 schema，路径留在 workspace，命令/写入符合安全策略。
- 审计资源：限制步骤数、每步重试次数、总工具调用、总 LLM 调用和计划版本数，防止无限循环。
- 审计失败必须给出结构化原因，不能静默修正或绕过。

#### Executor 与 Verifier

- Scheduler 只能从所有依赖成功的步骤中确定性选择 `ready` 步骤。
- Executor 在步骤作用域内向 LLM 请求 `ActionProposal`，审计通过后复用唯一的 `ToolInvoker` 执行。
- 保留既有 `ToolCallStartEvent`、`ToolCallSuccessEvent`、`ToolCallFailedEvent` 和 Trace；不得创建第二套工具事件或传输通道。
- Verifier 根据 `acceptance_criteria` 做通用验证，初期至少支持文件存在/内容断言、命令退出码、测试命令、工具结果断言和显式产物清单。
- 工具失败或验收失败进入 `retryable` 或 `replan_required`，不得直接让 run 成功或失败。

#### Resolver

- 输入失败事实、工具错误、当前步骤、计划摘要、预算和相关记忆，而非只有自由文本错误。
- 仅输出 `retry`、`revise_step`、`replan`、`abort` 四种受限决策。
- `retry` 必须受每步预算限制；`revise_step`/`replan` 形成 `PlanPatch`，经 Auditor 后创建新计划版本；历史版本只读保留。
- 达到预算、不可恢复错误或审计拒绝后，以明确 `reason` 失败结束。

#### Memory

- 工作记忆：复用 `ExecutionContext`，定期生成摘要，避免把原始工具输出无限追加到上下文。
- 情景记忆：保存已完成 run 的目标、批准计划、失败类型、有效修复、验收结果和最终摘要。
- 项目记忆：按 workspace 隔离，保存经验证的目录规则、Shell 约束、测试命令和工具偏好。
- 初期使用 JSON/JSONL 或现有 run 文件进行结构化检索；不强制引入向量数据库。只有通过 Verifier 或用户确认的事实才能进入长期记忆。

### 持久化、事件与界面

每个 run 在既有 `runs/<run_id>/events.jsonl` 外保存：

```text
runs/<run_id>/
  events.jsonl
  plan/
    plan.v1.json
    plan.v2.json
    state.json
    attempts.jsonl
  summary.json
```

新增领域事件统一定义在 `core/bus/events.py`，并复用 `EventBus → IpcEventBroadcaster → EventPushEnvelope`：

```text
plan_proposed, plan_approved, plan_rejected,
step_ready, step_started, step_verified, step_retrying,
plan_revised, memory_recalled, memory_recorded
```

CLI 不新增平行执行路径。TUI 作为主前端，应展示当前计划版本、步骤状态、失败原因、重试次数和验收结果，同时保留日志流与工具详情。

### 迁移顺序

1. 实现契约、PlanStore、状态机与计划版本持久化，为 `TaskManager` 提供迁移适配。
2. 实现 Planner + Auditor，完成 DAG 校验、复杂度策略和计划批准事件。
3. 实现 Scheduler + Executor + Verifier，将当前 AgentLoop 收敛为单步骤执行器并落地通用验收门禁。
4. 加入 Resolver，完成失败分类、有限重试、计划修订与预算终止。
5. 加入 working/episodic/project 三层结构化记忆与检索；之后再评估向量检索。
6. 完成 TUI 计划视图、恢复/回放能力，并清理仅服务旧 S4 关键词补丁的逻辑。

每一步必须可以独立运行全量测试；不得一次性替换 AgentLoop。旧 S4 的 `task_*`、历史 `.tasks` 文件及 S2/S3 daemon 协议必须保持可读取、可观测和向后兼容。

### 验收标准与测试要求

1. 复杂目标能生成经审计的无环 DAG；每一步均有依赖、允许工具和验收条件。
2. LLM 的空响应、无工具调用或主观“完成”声明，不能使未验收任务成功结束。
3. Scheduler 只执行依赖已满足的步骤；非法状态转移产生明确错误并保留历史状态。
4. 文件、测试、命令等交付由结构化验收条件验证，而不是目标文本关键词特判。
5. Resolver 可在预算内重试或提交经审计的计划修订；超过预算时以明确原因失败。
6. 中断后可从 run 目录回放计划版本、步骤状态、尝试与验收证据。
7. 记忆按 workspace 隔离，只有已验证事实进入项目记忆，检索不会无限膨胀上下文。
8. 现有 `agent.run`、事件订阅/回放、CLI、TUI 与 S3 Trace 回归全部通过。

测试至少覆盖：DAG 环检测、状态机、动作审计、路径/工具权限、Verifier、Resolver 预算、Memory 隔离；确定性 LLM 替身覆盖计划批准、依赖调度、失败重试、计划修订、未验收交付拒绝；以及 daemon 事件、Trace、重连回放和 TUI 计划状态的集成回归。

## S5：会话化交互与分层记忆体系（持续会话版）

### 一、阶段核心目标

将原有的「单次独立 run 触发」的命令式交互，升级为「常驻会话 + 分层记忆」的持续交互模式。用户在 TUI 或 CLI 会话中可连续输入目标，同一会话内的所有 run 共享统一 Session 上下文，自动沉淀短期对话历史与长期结构化笔记，实现跨轮次的上下文继承与经验复用。

全程复用 S0–S4 的 daemon 基座、IPC 协议、Agent 执行内核、五层自主规划与全链路 Trace 体系，仅在会话层与记忆层做扩展，保持全链路向下兼容，不破坏任何既有接口与行为。

### 二、核心架构改造要求

1. **CoreApp 集成 SessionManager**
   - 在 `manius-core` daemon 生命周期内维护全局 `SessionManager` 单例，支持多会话并发（不同客户端可创建独立 Session）。
   - Session 可与客户端绑定，也支持通过 `session_id` 跨连接恢复；每个 Session 内部可发起多次 agent run，共享 thread 上下文与 notes 记忆。
   - 不改动原有 `agent.run` 接口的行为，单次 run 仍可独立调用；新增 `session.*` 系列 RPC 方法管理会话生命周期与消息发送。
2. **Session 容器统一上下文**
   - 每个 Session 维护独立的 Thread 记忆（对话级短期上下文）与 Notes 记忆（长期结构化沉淀），每次新建 run 时自动注入对应上下文。
   - Session 内的 run 结束后，自动将本次 run 的核心结论与产物摘要回写至 Thread 历史，并可通过工具主动沉淀为 Note。
3. **记忆体系分层落地**
   - 采用 Thread + Notes 双层记忆架构：Thread 负责会话内连续对话的上下文连贯性，Notes 负责跨 run 的可复用知识沉淀。
   - 记忆存储与会话绑定，持久化至独立目录，支持历史会话回溯与恢复。
4. **工具集扩展**
   - 新增 `NoteSaveTool`，允许 Agent 在执行过程中将关键信息、结论、规则主动保存为长期笔记，供后续轮次检索使用。
   - 原有 8 个工具全部保留，在 Session 作用域内能力不变；记忆检索在 run 启动前由 Session 层自动注入，不占用工具调用配额。
5. **交互端全面升级**
   - `manius-tui` 新增底部常驻输入框，启动后自动创建默认会话，支持连续输入、流式展示、输入框自动重激活。
   - 新增 `manius chat` CLI 命令，提供终端交互式会话模式，作为 TUI 的轻量替代方案。

### 三、核心模块实现规范

#### 3.1 SessionManager

##### 定位

daemon 级会话管理器，负责 Session 的创建、查询、销毁、消息分发与生命周期管理，是客户端持续交互的统一入口。

##### 存储设计

- 每个 Session 对应独立目录：`sessions/<session_id>/`，包含 `meta.json`（会话元数据）、`thread.jsonl`（对话历史）、`notes/`（笔记文件目录）。
- Session ID 采用 UUID，创建时间、最后活跃时间、关联客户端 ID、关联 run\_id 列表写入元数据。
- 会话内产生的所有 run 仍保存在原 `runs/<run_id>/` 目录，仅在 Session meta 中维护 run\_id 索引，实现会话与 run 的双向关联，不重复存储事件数据。

##### 核心方法

1. `create_session(client_id=None) -> SessionMeta`
创建新会话，初始化目录、空 thread 与笔记空间，返回 session\_id 与会话元数据。
2. `send_message(session_id, content) -> str`
在指定会话内发起一次新的 Agent 运行：

- 从 Session 中提取 Thread 上下文与相关 Notes，组装为背景前缀注入本次 run。
- 复用原有 `AgentRunner` 与 S4 自主规划链路执行任务，立即返回 `run_id`，后台异步执行。
- run 结束后自动将本次交互的目标与最终摘要追加到 `thread.jsonl`。

3. `get_session(session_id) -> SessionMeta`
查询会话元数据、历史 run 列表与摘要。
4. `destroy_session(session_id)`
销毁会话内存实例，保留磁盘持久化数据。
5. `list_sessions() -> list[SessionMeta]`
列出所有历史会话，支持按最后活跃时间排序。

#### 3.2 分层记忆体系

##### Thread 记忆（会话短期记忆）

- **定位**：保存当前会话内所有轮次的「用户输入 - Agent 最终结论」摘要，保障连续对话的上下文连贯性。
- **存储格式**：`sessions/<session_id>/thread.jsonl`，每行一条记录，包含 `role`（user/assistant）、`content`（摘要文本）、`run_id`（assistant 角色关联）、`timestamp`。
- **注入逻辑**：每次 `send_message` 创建新 run 时，自动将最近 N 轮 thread 摘要拼接到 LLM 系统提示词中，轮数上限可配置，避免上下文溢出。
- **更新时机**：每次 run 正常结束后，由 Session 层自动提取 run 最终总结写入 thread，不依赖 LLM 主动调用。

##### Notes 记忆（长期结构化记忆）

- **定位**：Agent 主动沉淀的可复用知识，如项目规则、目录结构、常用命令、错误解决方案等，支持同会话内跨 run 复用。
- **存储设计**：每个笔记为独立 JSON 文件，存储于 `sessions/<session_id>/notes/note_<自增ID>.json`。
- **笔记字段**：`id`(int)、`title`(str)、`content`(str)、`tags`(list\[str\])、`source_run_id`(str)、`created_at`、`updated_at`。
- **检索逻辑**：新 run 启动前，基于用户目标关键词对当前会话的 notes 做轻量匹配，将 Top-K 相关笔记注入系统提示词的「已知背景」区块。
- **持久化边界**：本期仅实现会话内 notes 共享，不做跨会话自动迁移，为后续全局 workspace 级记忆预留扩展空间。

#### 3.3 NoteSaveTool（笔记保存工具）

- **封装能力**：封装 Session 内的笔记写入接口，允许 Agent 在执行过程中主动保存关键信息。
- **入参**：`title`（笔记标题）、`content`（笔记正文）、`tags`（可选标签列表）。
- **返回**：创建成功的笔记 ID 与基础信息。
- **权限约束**：仅允许写入当前会话的 notes 目录，本期不开放修改、删除能力，避免误操作。
- **事件兼容**：复用原有 ToolCall 事件体系，调用过程通过标准工具事件广播，TUI/CLI 可直接观测。

### 四、Agent 执行链路适配

1. **完全复用 S4 执行内核**
Session 层仅负责上下文注入与结果沉淀，不修改 `AgentLoop`、五层自主规划、工具调用与事件广播的内部逻辑。每次会话内的消息触发，本质仍是一次标准的 `agent.run`，仅额外携带会话上下文。
2. **会话内 run 标准执行流**
   1. 用户在 TUI/CLI 输入目标，通过 `session.send` RPC 发送到 daemon。
   2. `SessionManager` 匹配对应会话，检索 thread 历史与相关 notes，组装成上下文前缀。
   3. 创建新 run，将上下文注入 `ExecutionContext` 与 LLM 系统提示，启动 `AgentRunner` 执行。
   4. 执行过程中，Agent 可调用 `NoteSaveTool` 主动沉淀笔记。
   5. run 结束后，`SessionManager` 自动提取最终摘要写入 thread 记忆。
   6. 事件流推送完成后，客户端输入框重新激活，等待下一轮输入。
3. **上下文隔离与继承规则**

- 不同 Session 之间上下文完全隔离，记忆不互通。
- 同一会话内的 run 仅继承 thread 摘要与 notes，不继承完整的原始对话 token 与工具调用细节，控制上下文长度。
- 原有独立 `agent.run` 调用不受影响，不关联任何 Session，无额外记忆注入。

### 五、TUI 交互升级：底部输入框 + 持续会话

#### 5.1 整体布局调整

保留原有顶部状态栏与中部事件滚动区，底部新增固定高度的输入栏：

```
┌─────────────────────────────────────────┐
│  状态栏：连接状态 / 当前 Session ID     │
├─────────────────────────────────────────┤
│                                         │
│  事件滚动区（运行日志、工具调用、LLM 输出）│
│                                         │
├─────────────────────────────────────────┤
│  > 在此输入目标，回车发送                │
└─────────────────────────────────────────┘
```

- 输入栏常驻底部，不随事件区滚动；事件滚动区高度自适应，自动为输入栏预留空间。
- 启动 `manius-tui` 时自动向 daemon 请求创建默认 Session，状态栏同步显示当前 Session ID。

#### 5.2 输入交互逻辑

- **发送触发**：输入框聚焦状态下按 Enter 发送内容，发送后输入框置为禁用状态，避免重复提交。
- **流式展示**：事件流与原有逻辑完全一致，LLM token 流式追加、工具调用块折叠展开均保持原有体验。
- **自动重激活**：收到当前 run 的 `run_finished` 事件后，自动清空输入框并重新激活聚焦，准备下一轮输入。
- **快捷键保留**：`q` 退出逻辑不变，退出时自动清理客户端订阅，daemon 侧保留会话持久化数据。

#### 5.3 会话状态展示

- 状态栏新增当前会话的轮次计数提示。
- 不同轮次 run 之间用细分割线视觉区分，不破坏原有流式阅读体验。
- 所有事件仍按时间线追加到滚动区，兼容 S3 全链路追踪与 S4 规划事件的展示。

### 六、CLI 配套：`manius chat` 交互式命令

#### 命令定位

提供终端原生的交互式会话模式，适合无 TUI 环境下的连续交互，作为 `manius-tui` 的轻量替代方案。

#### 交互逻辑

- 执行 `manius chat` 后，自动创建 Session，进入 REPL 模式，提示符显示 `manius> `。
- 用户输入目标后回车，后台调用 `session.send`，实时打印事件流与 LLM 输出，逻辑对齐 `manius run` 的终端展示。
- 单次 run 结束后，提示符重新出现，支持继续输入下一轮。
- 输入 `exit` 或 Ctrl+C 退出会话，本地进程退出，daemon 侧保留会话持久化数据。
- 支持 `--session-id <id>` 参数，恢复接入已有历史会话。
- 支持 `manius resume <session-id>` 恢复对话，加载上下文。

### 七、RPC 接口与事件体系扩展

#### 7.1 新增 JSON-RPC 方法

在 `CoreApp` 中注册以下会话相关方法，全部复用现有 `SocketServer` 与 JSON-RPC 2.0 协议：

- `session.create`：创建新会话，返回 `session_id`。
- `session.send`：向指定会话发送消息，立即返回 `run_id`，后台异步执行。
- `session.get`：获取会话元数据与历史摘要。
- `session.list`：列出所有历史会话。
- `session.destroy`：销毁指定会话的内存实例。

#### 7.2 新增领域事件

在 `core/bus/events.py` 中新增会话与记忆相关事件，全部复用 `EventBus → IpcEventBroadcaster` 链路广播：

- `session_created`：会话创建事件，携带 `session_id` 与元数据。
- `session_message_sent`：会话内新消息提交，关联 `run_id`。
- `note_saved`：新笔记保存成功，携带笔记 ID 与标题。
所有事件均包含 `session_id` 字段，原有业务事件保持不变，客户端可按需过滤。

#### 7.3 Trace 埋点扩展

在 S3 全链路追踪体系中新增会话层埋点：

- `CLIENT->CORE` / `session`：`session.*` 系列请求入口。
- `CORE>CLIENT` / `session`：会话相关响应。
- `CORE` / `session`：会话创建、消息分发、记忆写入等内部事件。
所有追踪记录写入原有全局 `daemon.jsonl`，不新增追踪文件，保持单时间线调试能力。

### 八、设计原则与验收标准

#### 设计约束

1. **向下兼容**：原有 `agent.run`、`event.subscribe` 等所有接口行为不变；单次命令式调用与会话式交互并行可用。
2. **记忆分层清晰**：短期 Thread 与长期 Notes 职责明确，自动注入与主动保存边界清晰，不混淆执行上下文与持久记忆。
3. **无侵入执行内核**：Session 层作为包装器存在，不修改 `AgentRunner`、自主规划与工具调用的内部逻辑。
4. **持久化可回溯**：所有会话、记忆、run 数据均落盘，支持重启 daemon 后恢复会话与历史查看。
5. **交互自然流畅**：TUI 输入框无卡顿，流式输出不闪烁，轮次切换平滑，符合终端交互直觉。

#### 验收用例

执行命令：

```
# 终端 A：启动 daemon
uv run manius-core
# 终端 B：启动 TUI
uv run manius-tui
```

验收标准：

1. TUI 启动后自动创建 Session，底部输入框可用，状态栏显示连接正常与会话 ID。
2. 在输入框输入「读取 README.md 并总结」，按回车后事件区流式展示执行过程，工具调用与 LLM 输出正常。
3. 任务完成后输入框自动清空并重新激活，继续输入「基于总结生成项目结构说明文件」，Agent 可感知上一轮上下文。
4. 执行过程中 Agent 可调用 `note_save` 工具保存关键信息，会话目录下生成对应笔记文件。
5. 退出 TUI 重新启动，通过指定 `session_id` 可恢复历史会话，thread 历史与笔记仍然存在。
6. `manius chat` 命令可正常进入交互模式，连续多轮对话上下文连贯。
7. 原有 `manius run` 单次命令、事件订阅、全链路 Trace 全部功能正常，无回归问题。

#### 测试覆盖

- 单元测试：`SessionManager` 生命周期、记忆读写、RPC 参数校验、`NoteSaveTool` 逻辑。
- 集成测试：会话内多 run 上下文继承、笔记持久化与检索、TUI 输入交互、CLI chat 模式、RPC 接口兼容性。
- 回归测试：S0–S3 基础链路、S4 自主规划能力全部保持可用。

### 完成后能力边界

S5 完成后，系统从「单次任务执行工具」演进为「可连续对话的智能助手」，支持跨轮次上下文继承与主动知识沉淀。同时保留原有的命令式调用、全链路可观测、自主规划能力，为后续全局 workspace 记忆、多会话协作、多 Agent 协同等能力打下架构基础。