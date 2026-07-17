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

## s1

The current project is in the S1 development stage, and the S0 base has been completed, including configuration loading, logging system, TCP JSON-RPC SocketServer/SocketClient persistent connection communication framework, and basic CLI parsing framework.
S1 Core Objective: Implement the first end-to-end Agent operation pipeline, supporting the command: `uv run manius run --goal "summarize main sections of README.md"`
After running, two types of outputs will be achieved:
1. The terminal prints logs of Agent's thinking, planning, tool invocation, and step flow in real time (refer to the document for sample format);
2. Persist the full event records in runs/{run_id}/events.jsonl, saving all LLM interactions, tool invocations, and timing information.

### Required component checklist

1. CLI layer: Add a new run sub-command to parse the --goal parameter, serving as the entry point for the entire pipeline;
2. AgentRunner: Top-level assembly entry
 - Generate a unique run_id; automatically create a storage directory named "runs/{run_id}";
 - Initialize the EventBus; attach the subscribers StdoutPrinter and EventWriter;
 - Initialize the Context and AgentLoop, and inject all dependencies;
 - Responsible for initiating and closing tasks, and outputting the final summary statistics (total steps, total time consumed).
3. ExecutionContext: Global state container
 - Save the run metadata and initial task goal;
 - Maintain a complete history of multiple rounds of dialogues, with continuous appending of tool results and LLM responses for each round;
 - Record the current step number.
4. AgentLoop: The core driving loop, following the plan → act → observe loop paradigm
 - Repeat the process of planning, tool execution, and result observation;
 - Determine the termination condition: Exit the loop when the LLM indicates task completion;
 - Increment the step for each round of iteration; send events out in all stages.
5. LLM component AnthropicProvider
 Encapsulate LLM API calls; receive conversation context, initiate requests, and return structured responses; push events throughout the entire LLM interaction process.
6. Tool system
 ToolRegistry: Unified tool registration and retrieval;
 ReadFileTool: The only implementation tool, which reads local text files; 
 Specific tools Architecture: Each tool should have error messages that correspond to the execution errors of the tool. Furthermore, the specific execution of the tool needs to be decoupled from the broadcast event. Only the unified interface for tool invocation, wrapped by the broadcast event, should be exposed externally;
7. Event observation system (based on EventBus publish-subscribe)
 EventBus: Global Broadcast Center;
 StdoutPrinter: Subscribe to events and format output for human-readable terminal logs;
 EventWriter: Subscribe to events and persistently serialize the events into events.jsonl.

### Mandatory development constraints

There are only three packages (cli, core, tui) in the manius_code directory, and all the core functions in s1 are implemented in the core package.

1. Simplified architecture in S1 phase: manius run runs the Agent as an independent foreground process, **temporarily not using daemon IPC communication**. The SocketServer/SocketClient persistent connection components are retained but not integrated into the business in this round; IPC integration will be addressed in S2;
2. The tool only implements ReadFileTool and does not add any other tools;
3. LLM only implements Anthropic Claude docking, and does not design a multi-model abstraction layer for the time being;
4. Strict layering: The business agent logic, tools, LLM, and event observation are decoupled from each other, with dependencies injected through constructor functions;
5. All behaviors should not be hard-coded with print statements; **all outputs and logs must be broadcasted through the EventBus**; terminal printing and file persistence are uniformly implemented based on event subscriptions;
6. Define standard event data model: run_started / run_finished / step_planning / step_done / tool_call_start / tool_call_success / tool_call_failed;
7. The code adopts the asyncio asynchronous paradigm, utilizes pydantic for unified data validation, and follows standardized type annotations;
8. It is prohibited to implement S2 functions ahead of schedule: task background hosting, IPC remote calls, additional tools, web interface, and retry strategy.

Output delivery requirements:
1. Split the complete source code by modules, following a hierarchical directory structure;
2. Accompanying component dependency description;
3. Provide an example of the startup test command;
4. The relevant configurations for LLM have been filled in the .env file

## s2

### Engineering background
The current project has completed Phase S1: under a single process, 'manius run -- goal' can run an end-to-end Agent loop, including LLM calls, tool execution, event bus, terminal real-time printing, and events.json persistence for the entire chain.
We are now entering the S2 phase for architecture upgrade: we will migrate AgentRunner to the manius core daemon, convert CLI to a pure client, and remotely schedule tasks and subscribe to event streams through TCP JSON-RPC.

### Reusable assets (interfaces are strictly prohibited from modification and can be reused directly)
1. Core operating layer: Agentloop, ExecutionContext, ToolInvoke, AnthropicProvider, with completely unchanged interfaces and logic
2. Event system: All event models (AgentEvent family) EventBus、EventWriter、StdoutPrinter， The interface and rendering logic remain completely unchanged
3. IPC base: SocketServer, JSON-RPC 2.0 protocol architecture SocketClient， The existing ping interface is available
4. Persistence: The logic for writing events.json remains unchanged

##Core Objectives of S2 Stage
1. Make the manius core daemon the only agent execution carrier, and the CLI will no longer directly run AgentRunner
2. The event bus has added IPC broadcasting capability, and all running events can be pushed to subscription clients through sockets
3. Change the 'manius run' command of CLI to remote call mode: subscribe to events → initiate tasks → real-time consumption of event stream printing
4. Support multiple clients to subscribe to the same task event stream simultaneously

### Specific Renovation Task List
1. Daemon side adds IpcEventBroadcaster component
-As a subscriber of EventBus, receive all AgentEvents
-Serialize events into JSON line format and push them to all subscribed client connections through sockets
-Support concurrent subscriptions from multiple clients, automatic clearing of connection disconnections, and non blocking of event bus main processes
-The event push format is completely consistent with the existing event model fields, and the client can directly deserialize and reuse the StdoutPrinter

2. Daemon side SocketServer adds RPC method
-Add 'event. subscribe': After being called by the client, add the current connection to the event broadcast list and continuously push all subsequent running events
-Add 'agent. run': Receive goal parameter, instantiate AgentRunner and start task, synchronously return task start result
-All RPC strictly follow the existing JSON-RPC 2.0 protocol specifications and are consistent with the ping interface style

3. Daemon side AgentRunner access
-Maintain the AgentRunner lifecycle within the daemon process and reuse the S1 complete running link during task execution
-All events generated during task execution are connected to the local EventBus and distributed to EventWriter (persistence) and IpcEventBroadcaster (remote push) simultaneously

4. CLI side run command transformation
-Remove the logic of directly instantiating AgentRunner
-Establish a socket connection to the daemon and perform the following steps in sequence:
1. Call 'event. subscribe' RPC to subscribe to the event stream
2. The backend starts the coroutine to continuously read socket push events, deserializes them, and feeds them to the StdoudPrinter for real-time printing
3. Call 'agent. run' RPC to initiate the task
-After the task is completed and the event stream push is finished, clear the connection and exit

### Strict constraints
1. It is prohibited to modify the external interfaces of S1's existing core modules, including but not limited to Agentloop, ExecutionContext, event model, etc EventBus、StdoutPrinter、ToolInvoker
2. All newly added logic must not disrupt the single process running capability of S1, and the local direct running mode remains optional
3. The event push protocol is completely aligned with the local event model fields, and the client can reuse the StdoutPrinter without adding parsing logic
4. IPC broadcasting must not block the main process of the event bus. Abnormal connections must be gracefully downgraded without affecting local task execution and persistence

### Delivery requirements
1. Output the complete IpcEventBroadcaster implementation code
2. Output the processing logic for adding two RPC methods to SocketServer
3. Output the complete entry code modified with the CLI run command
4. Explain the responsibilities of each newly added code and its calling relationship with existing modules
5. Verification criteria: After starting the daemon, the CLI can execute 'manius run -- goal' to trigger tasks normally, the terminal can print events in real time, and events.json can be written normally, which is consistent with the single process effect of S1
