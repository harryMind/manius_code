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