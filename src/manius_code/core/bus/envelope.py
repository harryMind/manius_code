from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]
    id: int | str | None
    method: str
    params: dict[str, Any] = {}


class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    result: Any


class JsonRpcErrorBody(BaseModel):
    code: int
    message: str


class JsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    error: JsonRpcErrorBody


# 创建符合 JSON-RPC 2.0 的错误响应。
def make_error(request_id: int | str | None, code: int, message: str) -> JsonRpcError:
    return JsonRpcError(id=request_id, error=JsonRpcErrorBody(code=code, message=message))
