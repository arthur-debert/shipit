"""shipit.harness — the agent-harness enforcement core (HAR01).

The pure decision logic behind the `PreToolUse` coordinator guard (ADR-0012):
resolve the acting **role** from the Claude Code hook payload, then decide
whether an `edit` **operation** is allowed. Mirrors the `shipit.prstate` shape —
a pure, side-effect-free core that unit-tests against captured payloads with no
I/O — so the thin `shipit hook pretooluse` boundary (verbs/hook/) only marshals
stdin/stdout around these functions.

WS01 shipped the thinnest end-to-end thread; WS02 lands the real ADR-0012
policy as three pure units: `role.resolve_role` (closed registry, the
empty-`agent_type`⇒`coordinator` rule), `policy.decide(role, path, is_code,
break_glass)` (the security matrix, break-glass an input), and
`codepath.is_code_path` (the HAR01 default classifier, converging on the
ADR-0007 toolchain map later). The boundary (verbs/hook/) reads the break-glass
env marker and logs each use.

WS03 adds the role-prompt generator (`prompts.render`): it composes the lex
fragments (a shared base + one overlay per role, under `shipit.data.roles`) into
the reduced per-role prompts + the `AGENTS.md` union, and `policy`'s
`COORDINATOR_DENY_REASON` is now the GENERATED coordinator slice (loaded from the
bundled `roles/generated/coordinator-prompt.md`), so the deny wall and the
injected coordinator prompt are the same text.
"""
