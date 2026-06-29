# Trees have two modes: write Trees and shared read-only Trees

A **Tree** now comes in two modes. A **write Tree** (today's) is provisioned one-per-Run,
read-write, with `.treeinclude` + pixi + sccache — for an implementer or shepherd that
mutates files. A **read-only Tree** is clone + checkout only (no pixi, files `chmod`
read-only) and is **shared per `(repo, branch)`** — for **reviewers** (claude / codex /
antigravity) that read the diff and code but never execute. The real axis is
**branch-pinned vs ambient**, not read vs write: *ambient* exploration stays in the main
checkout with **no Tree** (the surviving "explorer exempt"); a branch-pinned reviewer
*does* get a Tree, just a cheap shared read-only one.

## Context

ADR-0017 routes every Run through `shipit spawn subagent`, including PR **reviewers**. A
reviewer is branch-pinned (it must see a specific PR head) but **read-only** — it reads the
diff and surrounding code and posts a review; it never builds or writes. Giving it a full
write Tree (per-Run clone + `.treeinclude` secrets + pixi provisioning) would pay the whole
write-Tree cost for work that touches nothing.

The old "explorer exempt" rule (ADR-0014, `CONTEXT.md` **Tree ownership**) read as
"read-only work needs no Tree." The reviewer case shows that was the wrong cut: an explorer
is exempt because it is **ambient** (no branch), not because it is read-only. A reviewer is
read-only *and* branch-pinned, so it needs a checkout on that branch — just not a writable,
provisioned one.

## Decision

Two Tree modes:

- **Write Tree** (unchanged): one per write-Run; `git clone --reference --dissociate`
  (ADR-0014) + `.treeinclude` + `shipit install` / pixi + sccache; read-write. Consumers:
  **implementer**, **shepherd**.
- **Read-only Tree** (new): clone + `git checkout` only — **no** `.treeinclude`, **no**
  pixi/provisioning — then the working files are `chmod`'d read-only. It is **shared per
  `(repo, branch)`**: N reviewers on the same PR head share **one** cheap clone, which is
  safe precisely because none of them mutate it. Consumer: **reviewer**.

The exemption rule is restated on the correct axis:

- **ambient** (no branch — an explorer's open-ended investigation) → **main checkout, no
  Tree**;
- **branch-pinned read-only** (a reviewer) → a **shared read-only Tree**;
- **branch-pinned read-write** (implementer / shepherd) → a **per-Run write Tree**.

## Consequences

- A reviewer Run is cheap: no provisioning, and the clone is amortized across every
  reviewer on the same `(repo, branch)`.
- `cleanup.classify` (ADR-0014, `tree/cleanup.py`) gains a new case: a shared `review/`
  Tree is **reclaimable** when its PR is merged or closed **AND** no reviewer is still
  live against it. (A write Tree's reclaim rule — merged + clean + no-unpushed + aged — is
  unchanged.)
- The read-only `chmod` is a guardrail, not the security boundary: it catches an
  accidental write and keeps a shared clone trustworthy for its co-tenants.
- Naming mirrors the slash-branch namespace (context, not a new decision here): a write
  Tree is `…/<org>/<repo>/<EPIC>/<WS>/agent-<id>`; a shared read-only Tree is
  `…/<org>/<repo>/<EPIC>/<WS>/review/` — the branch is git's source of truth, the
  `<id>` is the per-write uniqueness backstop a shared review Tree doesn't need.
