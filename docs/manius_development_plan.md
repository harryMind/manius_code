# manius_development_plan.md

this is the detail plan of the manius_code, a local agent system, including s0 ~ s7 8 stages.

## s0

build two process: manius-core and manius. make they Perform process communication.After the user types "manius ping", CLI reads the configuration, connects to 127.0.0.1:7437, and sends out A single JSON-RPC request; the daemon reads this line using readline(), verifies it as a protocol model, and dispatches it to core.ping handler, and then write PongResult back. We will build the system station by station along this path.

manius-core live in: ..\src\manius_code\core
manius live in: ..\src\manius_code\cli

the final effect: I will open two terminal
```bash
# terminal A
uv run manius-core

# terminal B
uv run manius ping
```
then output: 
```bash
pong server=0.0.1 uptime=*mx latency=*ms
```