# manius_code

Manius 是一套基于 asyncio 构建、支持 IPC 远程调用的本地AI智能体框架。
采用 `plan → act → observe` 标准Agent循环，依托事件总线实现全链路可观测；使用 TCP JSON-RPC 作为进程间通信协议，支持守护进程后台托管智能体任务，配套结构化事件持久日志与命令行交互工具，适合本地自动化LLM任务。

## 阶段进展
- **S0（完成）**：底座空壳。配置、日志、TCP IPC服务端/客户端、CLI基础框架、`manius ping`
- **S1（开发中）**：端到端Agent运行。实现 `manius run --goal`，支持LLM多轮思考、文件读取工具，终端实时输出 + `events.jsonl` 持久记录
- **S2（规划）**：Agent任务托管至守护进程，CLI通过IPC远程调度后台任务

## 快速体验（S1就绪后）
```bash
uv run manius run --goal "总结 README.md 的主要章节"
```

## 核心亮点
- 全程异步架构，支持并发RPC与服务端事件推送
- 统一事件驱动：终端实时输出、结构化日志复用同一套事件模型
- 可扩展工具注册表、多轮智能体循环
- 守护进程IPC架构，支持任务后台长期运行