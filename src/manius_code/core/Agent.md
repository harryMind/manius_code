# Agent.md

Based on the existing SocketServer TCP JSON-RPC server code, we will conduct an architectural refactoring, focusing solely on adjusting the internal code structure. In the S0 phase, we will not introduce additional components such as trace, broadcast, or contextvar. We will strictly adhere to four objectives:

## Transformation Goals
1. Concurrent model transformation:
Reconstruct the original connection serial processing message; after _read_loop receives an NDJSON message, use asyncio.create_task to schedule _dispatch separately, to avoid blocking subsequent requests on the same connection by long-running handlers. Only add task scheduling, do not perform concurrency throttling, and retain basic exception handling.

2. Automatic serialization processing of return values:
When assembling the JsonRpcSuccess response, automatically recognize the handler return object:
If it is a pydantic BaseModel instance, call .model_dump(); directly pass through the dict/primitive type and stuff it into the result field.

3. Decouple the transport layer from the business Command model:
Remove the hard-coded command model validation in SocketServer._dispatch.
The SocketServer transport layer only passes the original `request.params` dict[str, Any] to the handler;
All parsing and validation of request parameters using the Pydantic model should be moved to the upper-level business handler;
Synchronously adjust the CommandHandler signature: the input parameter is the original params dictionary, and the return value can be any serializable object.

4. Responsibilities are divided into two independent classes, completely decoupling network transmission from business:
① SocketServer (Daemon server): It is solely responsible for TCP listening, connection management, NDJSON message receiving and sending, JSON-RPC2.0 envelope parsing and packaging, message task scheduling, method routing and dispatching, and standard error construction; **it does not contain any business logic**. It retains port occupancy detection, graceful shutdown, and a full set of JSON-RPC standard errors.
② SocketClient (client, **long connection architecture, fully utilizing pending future + run_event_loop design**, referring to the provided example code structure):
- The architecture is fully preserved: connect, close, send_command, run_event_loop, _pending request mapping, UUID generation for request_id, IpcError exception encapsulation, and message_dispatch dispatch logic;
- The architecture reserves a server-side event push interface named "on_event";
⚠️ Important constraint: In the S0 stage, the upper-layer business (callers such as CLI ping) **temporarily does not use event push capabilities and does not register any event handlers**, only using basic RPC capabilities; do not simplify it to a short connection implementation.
Separate network transmission code to achieve reuse of communication logic.

## Mandatory constraints
1. The communication protocol remains unchanged: NDJSON, with the newline character \n serving as the message delimiter, adhering to JSON-RPC 2.0;
2. It is prohibited to transform SocketClient into a short-connection version. Instead, maintain the long-connection mechanism, support multiple concurrent RPCs, and adhere to the entire mechanism where request responses are matched through IDs;
3. Only refactor the architecture structure, and prohibit introducing additional components such as TraceWriter, IpcEventBroadcaster, and ContextVar;
4. Standardize type annotations and native asyncio syntax;
5. SocketServer external interface: server.register(method_name, handler), maintaining method→handler routing;
6. Boundary: The transport layer only handles the network and JSON-RPC envelope; the CoreApp business layer defines the Command model, and the handler internally completes the conversion from the params dictionary to the business model verification.

Output content:
1. Refactor and complete the complete source code of SocketServer
2. Complete source code for the long-lived connection version of SocketClient (consistent with the example architecture, retaining on_event but not requiring upper-layer usage)