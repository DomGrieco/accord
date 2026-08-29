# Hermes Human Gate

Durable human approval cards for consequential [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool calls.

Human Gate turns a tool call into a persistent decision instead of holding a live model turn open. The owner can approve, deny, or comment later. Approval and comment resume the originating Hermes session. Denial closes the request and any matching live originating session without waking the agent. An approval authorizes one exact replay of the captured call.

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
        +-- Deny    -> close the request and matching live session
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
- Approve, deny, comment, cancel, retry resume, and retry session stop
- Exact canonical call hashes and one-use claims
- Hidden continuation into the originating stored session
- Deterministic mock effect and publisher
- X as the first optional publication adapter
- No automatic or permanent approvals

## Explicit tool policies

Installing or enabling Human Gate does not gate any external write or post tool by default. The deterministic `human_gate_demo_effect` is the only built-in policy. Configure each real tool by exact name in the active profile after reviewing its argument schema:

```yaml
plugins:
  entries:
    human-gate:
      settings:
        policies:
          - tool_name: x_create_post
            effect_kind: publish
            display_fields: [account, text, quote_post_id]
            replay_fields: [account, text, quote_post_id]
```

Human Gate rejects unknown policy keys, duplicate fields, secret-bearing projection names, and replay fields the card does not display. The approval digest still covers the full original argument envelope. A changed tool, profile, stable session lineage, or argument cannot use an earlier approval. When Hermes supplies a stable `session_key`, the plugin persists it as both the resume target and approval lineage. Configured tools fail closed if Hermes supplies no session identity.

Cold resume across a replaced agent runtime still needs Hermes to pass its stable stored session key into tool hooks and middleware. The current host integration gate is recorded in [the stable session lineage owner note](docs/owner-gates/stable-session-lineage.md).

Hook and middleware interception cannot secure a raw effect tool if the gate can be disabled or bypassed. For a hard publication boundary, remove raw write credentials and tools from the model, and put the one-use claim check inside the owned publishing effect.

Read the [v0.1 specification](docs/specs/v0.1.md), [threat model](docs/threat-model.md), and [implementation plan](docs/plans/v0.1.md).

## Status

Runtime implementation remains local and unpublished while development is in progress.

## License

MIT
