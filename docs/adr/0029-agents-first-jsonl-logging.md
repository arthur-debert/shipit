# Agents-first JSONL logging with domain-key correlation

Agents are the primary consumers of shipit's durable record — shipit is a
self-building machine operating two orchestration layers deep — yet the log
was freeform text with no correlation, no timing, and `print()` as the only
trace of whole subsystems. We decided the file log is **JSONL**: one JSON
object per record with a human-readable `msg` inside and flat top-level
fields — `ts`, `level`, `logger`, `msg`, plus domain keys (`session`, `tree`,
`pr`, `run`, `repo`) present-when-bound (absent, not null). Correlation is
**domain keys only, no synthetic trace/span ids**: the domain already
correlates a parent and its detached child (same philosophy as "the PR + the
check run ARE the store", ADR-0005), and agents query by the nouns they care
about (`jq 'select(.pr==231)'`), not by trace handles they'd first have to
discover. Console stderr stays human-formatted; humans read the durable log
through `shipit logs`, which renders the JSONL. Hard cutover, no dual-format
period. Implemented over **structlog** (zero-dep, exceptionally maintained):
its `ProcessorFormatter` attaches to the existing `logsetup.py` handlers
(rotation, platformdirs paths, idempotent naming all survive), the processor
chain is the one seam for context-merge → redact → render, and
`foreign_pre_chain` lets untouched stdlib call sites participate — so the
logging migration can proceed subsystem-by-subsystem instead of big-bang.
Cross-process, bound keys pass to child shipit processes via the environment
and are rebound at logging setup (no package exists for this; ~10 lines at
the spawn/detach seams).

## Considered options

- **OTel log data model** (`severity_text`, nested `attributes.*`) — rejected:
  the eval record's `gen_ai.*` borrow filled a naming vacuum; log records are
  the opposite case, and flat fields win the jq ergonomics that agents-first
  optimizes for.
- **Synthetic trace ids** — rejected for now as redundant bookkeeping over
  domain keys; revisit only on a demonstrated slicing gap (adding them later
  is easy; removing them later is not).
- **loguru** — rejected: wants to own the whole pipeline, would delete rather
  than extend `logsetup.py`; stale maintenance. **python-json-logger** —
  viable formatter-only fallback, but leaves contextvars binding and
  dual-rendering hand-rolled; structlog deletes more of the subtle code for
  the same zero-transitive-dep cost.
- **Redaction packages** — none credible (dead, fragile, native-dep, or GPL),
  and exact-value masking of `secretsrc`-fetched secrets is served by no
  package; the redactor stays a small in-repo processor (ADR-0028).
