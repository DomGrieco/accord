# Owner gate: stable session lineage from Hermes

Status: the plugin side is ready. Live Hermes 0.20.6 does not yet pass a stable stored session key into `pre_tool_call` hooks or `tool_execution` middleware.

## Why this blocks cold resume

Hermes currently passes the agent runtime `session_id` and a per turn `task_id`. A resumed stored Desktop session can create a new runtime `session_id`. If Human Gate binds approval to that runtime ID, the approved digest cannot match after cold resume. If it omits lineage, an approval could cross sessions.

Human Gate now accepts `session_key` from both hook paths. When present, it uses that stable key for the persisted resume target and canonical approval lineage. It rejects configured tools with `human_gate_unroutable` when neither a stable key nor the legacy `session_id` exists. It no longer writes a shared sentinel lineage.

## Verified host gap

In the installed Hermes Agent source at `/Users/eru/.hermes/hermes-agent/agent/tool_executor.py`, `_dispatch_pre_tool_call_hooks` and `run_tool_execution_middleware` receive `session_id`, but neither call receives `_gateway_session_key` or another stored session key. The Hermes event hook docs show that gateway session events already use `session_key`.

## Owner decision

Authorize a separate Hermes core slice that widens the generic plugin hook and middleware context with a stable `session_key`, sourced from the current stored conversation identity. That change should use Hermes upstream TDD and exact head review. Do not special case Human Gate in core.

Until that generic host field exists, keep live publication policies disabled. The deterministic demo effect remains safe for local panel work.
