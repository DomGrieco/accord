# AGENTS.md

## Mission

Build Hermes Human Gate as a safe, auditable, open-source Hermes Agent plugin for durable human decisions on consequential tool calls.

## Product boundaries

- Hermes remains the control plane and source of agent intent.
- A human decision never depends on a blocked live model turn.
- Pending decisions persist until the owner approves, denies, comments, or cancels them.
- Approval binds to one exact canonical tool call and can be consumed once.
- Comment requests changes and resumes the originating session. It never grants authority.
- Deny terminates the request and resumes the originating session with the denial.
- The plugin must not claim that hook-only interception is a complete security boundary. Strong enforcement belongs inside an effect tool owned or wrapped by the gate, with raw alternatives removed.

## Safety rules

- Never execute a consequential action when state is missing, corrupt, ambiguous, stale, or cannot be locked.
- Never log, return, persist, or commit secret values.
- Hash the full canonical argument envelope for exact matching. Persist only adapter-approved display and replay fields.
- Do not store unrestricted arbitrary tool arguments by default.
- Never treat chat text such as "approved" as authority.
- Never provide session-wide or permanent approval for a consequential effect.
- Claim an approval before dispatch. A crash after claim produces `uncertain`, never an automatic retry.
- A changed tool name, account, target, text, media digest, or argument creates a new request.
- All provider and live-effect tests are opt-in. Default tests use deterministic local fixtures.

## Engineering rules

- Support Python 3.10 and newer.
- Use the Python standard library for the runtime core.
- Keep persistence, canonicalization, policy, session continuation, Desktop API, and effect adapters separate.
- Use SQLite transactions for state transitions and one-use claims.
- Tool handlers accept `args: dict` and `**kwargs`, catch errors, and return JSON strings.
- Use TDD for behavior changes. Preserve failing and passing commands in implementation notes.
- Keep schemas narrow and versioned.
- Use bounded strings, bounded JSON payloads, and explicit redaction.
- Public examples must contain no private client, employer, family, financial, medical, credential, or personal account data.

## Repository layout

- `plugin.yaml`: Hermes agent plugin manifest.
- `__init__.py`: thin Hermes registration layer.
- `human_gate/`: runtime package.
- `dashboard/`: profile-scoped FastAPI routes.
- `desktop/`: Hermes Desktop plugin.
- `docs/specs/`: product contracts and success criteria.
- `docs/adr/`: architecture decisions.
- `tests/`: unit, integration, plugin contract, and opt-in live tests.

## Verification

Run from the repository root:

```bash
uv run --python 3.11 pytest -q
uv run --python 3.11 ruff check .
uv run --python 3.11 mypy human_gate
hermes plugins doctor . --ci
```

Before release:

1. Run all checks from a clean checkout.
2. Prove pending cards survive process and Desktop restarts.
3. Prove approve consumes only the exact matching call once.
4. Prove deny and comment cannot execute an effect.
5. Prove a claimed call recovered after a crash becomes `uncertain`.
6. Prove no secret-bearing fields reach persisted display data or logs.
7. Run Desktop interaction tests for approve, deny, comment, and resume failure.
8. Do not run a live provider mutation without explicit owner approval for that exact test.
