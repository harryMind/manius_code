"""
this file aim to learn how we use json-rpc2.0 to remote call a func. And demonstrate 
the basic calls utilized in this project.
"""

# 1. 首先要定义JSON-RPC2.0的请求与响应结构
from pydantic import BaseModel,Field
from typing import Literal, Any

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str = Field(description="远程调用的函数名")
    params: dict[str,Any] = {}
    id: int | str | None

class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    result: Any

class JsonRpcErrorBody(BaseModel):
    code: int
    message: str

class JsonRpcFailed(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    error: JsonRpcErrorBody

import json
def make_error(request_id: int | str | None, code: int, message: str) -> JsonRpcFailed:
    return JsonRpcFailed(id=request_id, error=JsonRpcErrorBody(code=code, message=message))

# 包装一个JsonRpc2.0请求数据
def send_request(method: str,params: dict[str,Any],id: str):
    request = JsonRpcRequest(method=method,params=params,id=id)
    # 把pydantic对象转为JSON model_dump_json()
    json_str = request.model_dump_json()
    return json_str

# 2 创建两个进程并连接

# 连接这个主机地址
_HOST = "127.0.0.1"
_PORT = 8000

import threading
import time
import asyncio

def _now():
    return time.ctime()

# 用于客户端连接到服务器
async def connect(host: str,port: int):
    # 异步函数
    print(f"[{_now()}] 开始连接 {host}:{port}")
    return await asyncio.open_connection(host,port) # 返回reader 与 writer

import uuid
# 创建client统一管理长连接
class SocketClient:

    def __init__(self,host: str,port: str|int):
        self._host = host
        self._port = port
        self._reader = None
        self._writer = None
        self._pending: dict[str, asyncio.Future[JsonRpcSuccess]] = {}
        self._event_handlers = []

    def on_event(self, handler):
        self._event_handlers.append(handler)
    
    async def connect(self):
        if self._writer is not None:
            raise RuntimeError("SocketClient is already connected")
        print(f"[{_now()}] 开始连接 {self._host}:{self._port}")
        self._reader,self._writer =  await asyncio.open_connection(self._host,self._port) # 返回reader 与 writer
    
    async def send_command(self,method: str,params: dict[str,Any]) -> JsonRpcSuccess:
        if self._writer is None:
            raise RuntimeError("SocketClient is not connected")
        request_id = str(uuid.uuid4())
        future: asyncio.Future[JsonRpcSuccess] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future # 先占位Future是空容器 仅用来阻塞 `send_command`、后续接收线程回填结果。
        try:
            request = send_request(method,params,id=request_id)
            self._writer.write(request.encode() + b"\n")
            await self._writer.drain()
            return await future
        finally:
            self._pending.pop(request_id, None)
    
    async def run_read_loop(self):
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 标准JSON-RPC响应
                if "jsonrpc" in data and "id" in data:
                    rid = data["id"]
                    if rid in self._pending:
                        fut = self._pending.pop(rid)
                        try:
                            if "error" in data:
                                err = JsonRpcFailed.model_validate(data)
                                fut.set_exception(RuntimeError(f"RPC Err {err.error.code}: {err.error.message}"))
                            else:
                                res = JsonRpcSuccess.model_validate(data)
                                fut.set_result(res) # 这是给future设置结果
                        except Exception as e:
                            fut.set_exception(e)
                # 服务端主动推送事件（无id，纯通知）
                elif data.get("kind") == "event":
                    evt = data["event"]
                    # 执行事件的回调函数
                    for h in self._event_handlers:
                        await h(evt)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()

    async def close(self):
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._reader = None
        self._writer = None

# 模拟服务器处理
async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    print(f"[{_now()}] Server new client {addr}")

    # 后台定时推送模拟事件
    async def push_event():
        while True:
            await asyncio.sleep(2)
            evt_msg = json.dumps({"kind": "event", "event": {"type": "step.run", "msg": "agent step running..."}})
            writer.write(evt_msg.encode() + b"\n")
            await writer.drain()

    push_task = asyncio.create_task(push_event())

    try:
        while line := await reader.readline():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = raw.get("id")
            method = raw.get("method")
            resp_data = None

            # 模拟RPC接口
            if method == "manius.run":
                resp_data = JsonRpcSuccess(id=rid, result={"status": "start_ok"})
            elif method == "echo":
                resp_data = JsonRpcSuccess(id=rid, result=raw["params"])
            elif method == "ping":
                resp_data = JsonRpcSuccess(
                    id=rid,
                    result={"msg": "pong", "ts": time.time()}
                )
            else:
                resp_data = make_error(rid, -32601, f"unknown method {method}")

            writer.write(resp_data.model_dump_json().encode() + b"\n")
            await writer.drain()
    finally:
        push_task.cancel()
        writer.close()
        await writer.wait_closed()
        print(f"[{_now()}] Client {addr} disconnected")



async def start_server(host: str, port: int):
    server = await asyncio.start_server(handle_client, host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    print(f"[{_now()}] Server listen on {addrs}")
    async with server:
        await server.serve_forever()

def run_server_thread():
    asyncio.run(start_server(_HOST, _PORT))

async def client_main():
    client = SocketClient(_HOST, _PORT)

    # 注册事件回调，接收服务端推送
    async def print_event(evt):
        print(f"<<< Recv Event: {evt}")

    client.on_event(print_event)

    await client.connect()
    # client接收端必须要有一个读取结果的协程
    read_task = asyncio.create_task(client.run_read_loop())

    # ping服务端
    print("=== 调用 ping RPC ===")
    echo_res = await client.send_command("ping", {"text": "hello rpc"})
    print("ping result:", echo_res.result)

    print("=== 启动manius.run ===")
    run_res = await client.send_command("manius.run", {"goal": "read file demo.txt"})
    print("manius.run result:", run_res.result)

    # 等待接收事件5秒
    await asyncio.sleep(5)
    await client.close()
    read_task.cancel()

if __name__ == "__main__":
    # 子线程运行服务端
    server_th = threading.Thread(target=run_server_thread, daemon=True)
    server_th.start()
    time.sleep(0.3)

    # 主线程运行客户端异步逻辑
    asyncio.run(client_main())
