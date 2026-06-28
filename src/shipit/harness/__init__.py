"""shipit.harness — the agent-harness enforcement core (HAR01).

The pure decision logic behind the `PreToolUse` coordinator guard (ADR-0012):
resolve the acting **role** from the Claude Code hook payload, then decide
whether an `edit` **operation** is allowed. Mirrors the `shipit.prstate` shape —
a pure, side-effect-free core that unit-tests against captured payloads with no
I/O — so the thin `shipit hook pretooluse` boundary (verbs/hook/) only marshals
stdin/stdout around these functions.

WS01 ships the thinnest end-to-end thread: a closed role registry, the
empty-`agent_type`⇒`coordinator` rule, and a deliberately HARDCODED code-path
check (anything under `src/`). WS02 replaces the minimal `decide()` /
`is_code_path()` with the real ADR-0012 policy (break-glass input + the
path→toolchain classifier); the role resolver and the boundary are stable.
"""
