# ADR 0001: Durable block and exact replay

## Status

Accepted for v0.1.

## Context

Hermes native approval prompts wait inside a live tool call and expire. Human Gate needs decisions that can remain pending across restarts and that resume the original work later.

A plugin `pre_tool_call` hook can veto a call. Hermes also exposes stored-session resume and hidden prompt submission in Desktop. This allows the request and the decision to happen in separate turns.

## Decision

When a configured tool is first called, Human Gate will persist a request and return a block result. It will not keep a callback or model turn alive.

After any owner decision, Desktop will resume the exact originating stored session and submit a nonempty hidden continuation that includes any optional owner reason:

- approve: retry the exact call for request `<id>`;
- deny: the request was denied, do not execute it;
- comment: revise according to the owner comment and submit a new request if needed.
- cancel: the request or unclaimed approval was withdrawn, do not execute it.

Approval creates one exact replay authority. The next matching call is claimed before execution. Deny and cancel grant no authority and never execute the effect. Empty decision prompts are blocked before wake, and delivery is acknowledged only after the hidden prompt is accepted. Changed calls create new requests.

## Consequences

- Cards can remain pending forever without blocked worker threads.
- The model makes a fresh provider call after the owner acts.
- Resume can fail independently of approval and must be retryable.
- Compression may remove details needed to reconstruct a call, so adapters may store a bounded safe replay projection.
- Generic hook interception is weaker than enforcement inside an owned tool.
