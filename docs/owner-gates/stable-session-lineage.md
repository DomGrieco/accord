# Owner gate: stable session lineage from Hermes

Status: ready for local Desktop testing. The generic Hermes host slice exists locally on branch `feature/human-gate-session-key` at commits `4a27bb2781` and `94d576b4b9`. It has not been pushed or published.

## Why this blocks cold resume

Hermes currently passes the agent runtime `session_id` and a per turn `task_id`. A resumed stored Desktop session can create a new runtime `session_id`. If Accord binds approval to that runtime ID, the approved digest cannot match after cold resume. If it omits lineage, an approval could cross sessions.

Accord now accepts `session_key` from both hook paths. When present, it uses that stable key for the persisted resume target and canonical approval lineage. It rejects configured tools with `human_gate_unroutable` when neither a stable key nor the legacy `session_id` exists. It no longer writes a shared sentinel lineage.

## Local host integration

The local Hermes core slice passes `_gateway_session_key` through tool request middleware, `pre_tool_call`, tool execution middleware, `post_tool_call`, result transforms, deferred calls, `execute_code` nested tools, and local and remote persistent kernels. Existing positional hook and middleware calls remain compatible.

The Desktop wake path now requires `session.resume` to return all four fields below before it registers a continuation and submits the hidden decision prompt:

- a nonempty runtime `session_id`
- a nonempty stored `session_key`
- a nonempty `resumed` identity equal to `session_key`
- a nonempty `requested_session_id` equal to the stored session requested by the approval card

Missing or conflicting identity proof fails before `prompt.submit` and leaves the decision retryable. Hermes may resolve the requested stored session to a continuation tip, but it must return both the original request and the resolved tip. This lets the exact approved replay survive runtime replacement without crossing sessions.

## Safe Desktop owner test

1. Use the existing Life Desktop. Do not start a second server.
2. Command palette: Reload desktop plugins, then Open Accord approvals.
3. Click a different Life chat. Do not use the plugin-work thread that renamed Accord.
4. In Accord, click Queue demo in focused chat. Ignore parked card 336fc722.
5. Open the new pending card. Confirm Session is the resume target for that chat. Lineage appears only if it differs.
6. Choose Approve. Desktop must resume that stored session and submit a hidden decision prompt.
7. Let the resumed session retry the same demo call once. The card must move through approved and claimed to succeeded.
8. Repeat with a fresh card for Request changes, Deny, and Cancel. Each must resume with a nonempty hidden prompt. None may execute the demo effect.

Keep generic policies empty and the X adapter disabled during this test. Do not use a live provider mutation.
