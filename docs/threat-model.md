# Threat Model

## Protected outcome

A consequential external effect must not occur unless the owner approved the exact call shown on a durable card.

## Trusted components

For v0.1:

- Hermes Agent core and plugin loader
- Hermes Desktop and its authenticated gateway connection
- Accord runtime and SQLite store
- Accord owned effect adapters
- The local operating system and user account

## Untrusted inputs

- Model output
- Web pages, posts, documents, and retrieved text
- Tool arguments produced by an agent
- Provider responses
- Session summaries and compressed context
- Comments or text that merely claim approval

## Threats and controls

### Model retries or changes the request

Control: approval binds to the canonical tool name, profile, session lineage, effect kind, and complete argument digest. A mismatch creates a new pending request.

### Duplicate clicks or concurrent retries

Control: SQLite compare-and-swap transition from `approved` to `claimed`. Only one transaction wins. The owned handler then consumes a shared, lock-protected, thread-bound in-process claim before dispatch. Middleware revokes all copied contexts when execution scope ends and fails closed if the owned handler returned without consuming the claim.

### Process crashes after provider mutation

Control: claim before dispatch. Startup recovery changes abandoned `claimed` requests to `uncertain`. Never retry automatically.

### Provider call raises after claim

Control: an unclassified exception after claim becomes `uncertain`, since dispatch may have happened. An adapter may report a definitive failure only when it proves dispatch did not begin.

### Comment accidentally approves

Control: comment transitions only to `changes_requested`. The transition table has no path from comment to `approved` or `claimed`.

### Denied request is retried

Control: denied authority is never reusable. Policy may suppress an immediate identical retry, but it cannot execute.

### Agent forges an approval message

Control: chat content has no authority. Only a database decision written by the owner action endpoint can create `approved`.

### Sensitive arguments leak into the inbox

Control: adapters define display and replay projections. The full argument envelope is hashed but not persisted by default. Bounded redaction tests inspect the database and logs.

### Hook crashes or is disabled

Control: no hard-security claim for hook-only interception. Owned effect tools check the gate inside their handler and fail closed. Deployments must remove raw alternate write tools and credentials.

### Alternate shell or HTTP path bypasses the gate

Control: Level 3 deployments do not expose raw write credentials to the agent and disable alternate write tools. Terminal pattern blocking is defense in depth only and is never described as complete shell mediation.

### Desktop or gateway is offline

Control: cards and decisions remain in SQLite. Resume state becomes `failed`. No effect executes until an exact replay crosses the gate.

### Malicious local user or root process

Out of scope. This plugin does not sandbox the host owner or root.

## Security claims

### We claim

- Durable decisions do not time out.
- Approval is exact, one use, and fail closed on ambiguous state.
- Deny and comment grant no execution authority.
- Owned effect tools cannot execute without a valid claim.
- Crash recovery does not blindly retry a possibly completed effect.

### We do not claim

- A Python hook alone can secure arbitrary third-party tools when the plugin is disabled or crashes.
- Shell command matching prevents every bypass.
- Desktop disk plugins are sandboxed. Hermes documents that they have full renderer authority.
- The plugin protects against an attacker who controls the operating-system account.
