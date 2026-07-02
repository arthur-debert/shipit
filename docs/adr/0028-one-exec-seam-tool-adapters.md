# One Exec seam; one Tool adapter per tool

shipit's work is dominated by external commands (pixi, git, gh, backend CLIs),
and the subprocess boundary had grown four parallel conventions — `ProcError`,
two independent `GhError` classes, rc-tuple returns, `LaunchResult` — with no
logging, no timing, no timeout policy, and stdout dropped on failure. We
decided every subprocess call is an **Exec** (CONTEXT.md) through ONE runner:
per-Exec structured record (argv, cwd, rc, duration; both streams captured on
failure), one transport error `ExecError` (missing binary normalized into it,
not a raw `FileNotFoundError`), a generous overridable default timeout (5
minutes — nothing hangs by default; legitimate long-runners override, `None`
allowed), and central redaction (mask exact values registered by `secretsrc`
at fetch time, plus token/PEM patterns). Per-tool knowledge lives in exactly
one **Tool adapter** per tool, in the tool's domain home (pixi execution joins
`pixienv/`; `gh.py` and `prstate/ghapi.py` merge into one gh adapter) — no
physical `tools/` layer. Adapters harvest the most structured output the tool
natively offers (native JSON > porcelain formats > converted, e.g. `jc`) and
return existing core value objects (`PR`, `Repo`, `Sha`), never
adapter-shaped parallel types.

The "converted" rung was evaluated live on the `session/liveness` ps probe
(PROC02-WS04, epic #254): `jc` is adopted for JSON-less tools, with two
load-bearing caveats an adapter reaching for it must honor:

1. **jc's table parsers are header-driven, so the adapter must pin the tool's
   output shape.** Default headers differ per platform (`ps`'s `args` →
   `COMMAND` under procps) and multi-token fields (`lstart`) can't survive a
   whitespace table split — the liveness adapter pins one `-o` per column and
   uses the numeric `etime`. jc converts; it does not absolve the adapter of
   choosing a convertible, portable output format.
2. **jc validates nothing domain-shaped.** Garbage input yields `[]` or
   nonsense-keyed dicts, not errors — the adapter still owns the "is this row
   usable" checks (e.g. require int pid/ppid, degrade an unparseable field to
   a safe answer rather than raising).

Rule of thumb: reach for jc when the tool has no JSON/porcelain mode AND jc
has a parser for it; pin the tool's output format explicitly; keep validation
in the adapter.

## Considered options

- **Per-tool exception hierarchy** (`GhError(ExecError)`, `PixiError(...)`) —
  rejected: transport taxonomy nobody branches on (a caller knows which tool
  it called). Semantic subclasses only where a caller genuinely branches on
  meaning (e.g. missing binary).
- **No timeout default (explicit at every call site)** — rejected for a 5m
  default: the win is that nothing can hang silently; the cost is that known
  long-runners (backend launches, cold `pixi install` / `npm ci`) must carry
  explicit overrides, set once at their adapter/call site.
- **git/GitHub client libraries** (GitPython, pygit2, PyGithub, githubkit) —
  rejected: the git surface is mutation-dominated with tiny disciplined
  parsing; `clone --reference --dissociate` (ADR-0014) is poorly served;
  `gh` owns auth/pagination/GraphQL for free (same borrow-the-tool logic as
  ADR-0022).
- **Subprocess DSLs** (`sh`, `plumbum`) — rejected: the runner is small and
  the value is our conventions; a DSL fights the injectable-runner seam the
  tests depend on.

## Consequences

- Agent launches keep `LaunchResult` semantics as a consumer view over the
  runner — a nonzero child stays a normal lifecycle outcome (ADR-0019/0020),
  not an `ExecError`.
- The old error types and rc-tuple conventions are deleted with no aliases
  (no-backwards-compat).
- Any tool argv built outside that tool's adapter is a review defect — the
  two-`GhError` disease is now statable and checkable.
