# Hermes Human Gate

Durable human approval cards for consequential [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool calls.

Human Gate turns a tool call into a persistent decision instead of holding a live model turn open. The owner can approve, deny, or comment later. Approval and comment resume the originating Hermes session. Denial closes the request without waking the agent. An approval authorizes one exact replay of the captured call.

> [!WARNING]
> This project is under active development. It does not yet authorize live provider mutations.

## Intended flow

```text
Agent calls a gated tool
        |
        v
Human Gate stores an exact pending request
        |
        v
Hermes Desktop shows a non-expiring card
        |
        +-- Approve -> resume session -> allow one exact replay
        +-- Comment -> resume session -> ask agent to revise
        +-- Deny    -> terminate the request without waking the agent
```

## Enforcement model

Human Gate supports three levels:

1. **Interception:** a `pre_tool_call` hook blocks selected existing tools.
2. **Claim:** execution middleware consumes one exact approved receipt before dispatch.
3. **Owned effect:** a tool implemented by or integrated with Human Gate checks the receipt inside its handler and fails closed.

Level 3 is the security target for publishing. A hook alone cannot stop a disabled plugin or an alternate raw credential path. A real deployment must remove ungated write tools and credentials from the agent's reach.

## v0.1 scope

- Profile-scoped SQLite state
- Persistent Desktop approval inbox
- Approve, deny, comment, cancel, and retry resume
- Exact canonical call hashes and one-use claims
- Hidden continuation into the originating stored session
- Deterministic mock effect and publisher
- X as the first optional publication adapter
- No automatic or permanent approvals

Read the [v0.1 specification](docs/specs/v0.1.md), [threat model](docs/threat-model.md), and [implementation plan](docs/plans/v0.1.md).

## Status

Runtime implementation remains local and unpublished while development is in progress.

## License

MIT
