"""Objective run evaluation (HAR02) — the pure core of the eval wire.

At a **run**'s terminal lifecycle hook, deterministically read the on-disk
transcript + `.meta.json` and write one **eval record** per run to a
harness-owned local store (docs/legacy-prd/har02-run-eval.md, ADR-0013). Every field is
extracted *by code* — no model call, no judge (the subjective agent-as-judge is
HAR04).

The split mirrors ADR-0012's pure-core / thin-boundary shape:

  - :mod:`locate`     — resolve the just-closed run's transcript + meta (boundary).
  - :mod:`extractors` — objective metrics from the transcript (pure core).
  - :mod:`record`     — assemble the JSONL eval record (pure).
  - :mod:`store`      — the local, never-committed store FAMILY (boundary):
    the eval-record kind, plus the review-round record kind the review path
    writes (:mod:`shipit.review.roundrecord`, RVW02-WS03) — one convention,
    two record kinds.

The `shipit hook stop` / `shipit hook subagent-stop` boundary
(:mod:`shipit.verbs.hook.eval`) wires them together, synchronously and fail-open.
"""

from __future__ import annotations
