# Accord

Durable human approval cards for consequential [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool calls. This plugin was previously called Human Gate.

Accord turns a tool call into a persistent decision instead of holding a live model turn open. The owner can approve, deny, comment, or cancel later. Every decision resumes the exact originating Hermes session with a nonempty hidden prompt that includes any optional owner reason. Denial and cancellation never authorize or execute the effect. An approval authorizes one exact replay of the captured call until the gate claims it or the owner revokes it.

> [!WARNING]
> This project is under active development. It does not yet authorize live provider mutations.

## Intended flow

```text
Agent calls a gated tool
        |
        v
Accord stores an exact pending request
        |
        v
Hermes Desktop shows a non-expiring card
        |
        +-- Approve -> resume origin -> allow one exact replay
        +-- Comment -> resume origin -> ask agent to revise, no authority
        +-- Deny    -> resume origin -> report denial, no authority
        +-- Cancel  -> resume origin -> report cancellation, no authority
```

## Enforcement model

Accord supports three levels:

1. **Interception:** a `pre_tool_call` hook blocks selected existing tools.
2. **Claim:** execution middleware consumes one exact approved receipt before dispatch.
3. **Owned effect:** a tool implemented by or integrated with Accord checks the receipt inside its handler and fails closed.

Level 3 is the security target for publishing. A hook alone cannot stop a disabled plugin or an alternate raw credential path. A real deployment must remove ungated write tools and credentials from the agent's reach.

## v0.1 scope

- Profile-scoped SQLite state
- Persistent Desktop approval inbox with bounded decision and receipt history
- Approve, deny, comment, cancel, revoke unclaimed approval, and retry safe pre-dispatch resume failures
- Exact canonical call hashes and one-use claims
- Startup recovery marks abandoned claimed effects uncertain without retrying them. A process-held runtime lock prevents a second plugin process from recovering a claim that may still be executing.
- Hidden continuation into the originating stored session
- Deterministic mock effect and persistent idempotent mock publisher
- X as the first optional publication adapter
- No automatic or permanent approvals

## Explicit tool policies

Installing or enabling Accord does not enable live X posting. The built-in `accord_demo_effect` and `accord_mock_publish` tools are local fixtures and never contact a provider. Legacy `human_gate_*` tool names remain registered so pending cards survive the rename. Accord registers `x_create_post` as an owned effect, but its adapter fails before dispatch until the active profile explicitly enables one X account:

```yaml
toolsets:
  - hermes-cli
  - accord

plugins:
  enabled:
    - accord
  entries:
    accord:
      settings:
        x:
          enabled: true
          app: life
          account: DomAtSiteSage
```

`x_create_post` accepts the configured account, text of up to 280 characters, and an optional numeric quote post id. It invokes the profile's existing `xurl` OAuth2 account only after an exact one-use approval. The adapter stores no credential values. A timeout, nonzero provider response, or response without a valid post id becomes `uncertain` and is never retried automatically.

The plugin also intercepts simple `terminal` calls for `xurl post` and `xurl quote`. It hashes the complete terminal argument envelope, stores only the normalized account, action, text, and quote post id for review, and never replays the shell command. After approval, the middleware consumes the exact one-use terminal claim and dispatches through the same owned X adapter. Read-only `xurl` commands pass through. Raw API writes, shell composition, token extraction, other X mutations, and unsupported command forms fail closed without an approval request.

Other external tools require an explicit policy after their argument schemas are reviewed. Policies may use an exact `tool_name` or a `tool_glob`; exact tool names always outrank overlapping globs. `terminal` policies may add `command_exact` and `command_glob` lists, with exact commands always outranking overlapping command globs. Middleware blocks the matching call before `next_call`, persists a pending card, and allows only one exact approved replay. Accord rejects unknown policy keys, duplicate fields, secret-bearing projection names, and replay fields the card does not display. The approval digest covers the full original argument envelope. A changed tool, profile, stable session lineage, or argument cannot use an earlier approval. Resumed continuation tips are registered as aliases of the request's stable lineage so compression does not create a second approval card.

```yaml
plugins:
  entries:
    accord:
      settings:
        policies:
          - tool_glob: "records_*"
            effect_kind: records_write
            display_fields: [record_id]
            replay_fields: [record_id]
          - tool_name: terminal
            effect_kind: local_command
            display_fields: [command]
            replay_fields: [command]
            command_exact: ["printf human-gate-fixture"]
            command_glob: ["touch /tmp/human-gate-fixture-*"]
```

Approve, request changes, deny, and cancel each produce a nonempty hidden decision prompt, include any optional owner reason, and resume the request's exact originating profile/session. Deny and cancel never grant execution authority or execute the effect. Empty prompts are rejected before wake, and delivery is acknowledged only after Hermes accepts the prompt. A rejected delivery, including Hermes JSON-RPC code 4090 active-session capacity, remains retryable with an owner-visible error; an ambiguous lost response after wake is not automatically retried.

`accord_mock_publish` proves the owned publication path without a network call. Its exact approval envelope includes a mock destination, text, up to four media SHA-256 digests, an opaque idempotency key, and a test outcome. The publisher stores only hashes and a synthetic provider id. A response-loss fixture becomes `uncertain`; a newly approved retry with the same key returns the first synthetic result instead of creating a second publication.

Cold resume across a replaced agent runtime requires Hermes to pass its stable stored session key into tool hooks and middleware. The plugin rejects configured calls when Hermes supplies no usable session identity. Desktop accepts a resumed continuation only when Hermes returns matching `resumed` and `session_key` identities and echoes the exact requested `requested_session_id`. The local host integration and safe Desktop test steps are recorded in [the stable session lineage owner note](docs/owner-gates/stable-session-lineage.md).

Hook and middleware interception cannot provide an operating-system security boundary if the plugin can be disabled or same-user code can reach raw credentials. For a hard publication boundary, remove raw write credentials and tools from the model or isolate them behind a separately privileged service. The owned effect and terminal adapter protect normal Hermes tool execution but do not claim to sandbox malicious same-user code.

When the X adapter is enabled, Accord also gates every other terminal command. This closes unapproved indirect shell paths to the same-user `xurl` credentials. Safely parsed direct `xurl` writes still use the owned adapter after approval; unsupported shell-built `xurl` commands remain blocked.

Read the [v0.1 specification](docs/specs/v0.1.md), [threat model](docs/threat-model.md), and [implementation plan](docs/plans/v0.1.md).

## Status

Runtime implementation remains local and unpublished while development is in progress.

## License

MIT
