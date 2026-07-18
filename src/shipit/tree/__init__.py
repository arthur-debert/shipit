"""``shipit tree`` — isolated Trees, the substrate every agent **Run** works in.

ONE place to understand Trees end to end. The rest is split across ADR-0014/15/16/17/18/19
and the sibling ``spawn`` package; this docstring distills the current state so a future
agent reads it here, not across six issue bodies.

The problem — footgun-free isolated Runs:
    Concurrent agents (and the human) must never collide on one shared working tree, and
    an agent must never silently write into the *wrong* checkout. Claude Code's native
    ``git worktree`` feature fails both: it drops checkouts into source-controlled
    ``.claude/worktrees/`` and, worse, a subagent's Bash resets cwd to the *parent* repo
    every call — so writes meant for an isolated checkout land back in the main one (the
    bash-cwd footgun). A **Tree** is the fix: a fully-independent ``git clone --reference
    --dissociate`` (its own ``.git``, can sit on ``main``) under a central root OUTSIDE
    any repo (``~/workspace/trees/<org>/<repo>/…``, ADR-0014), NOT a git worktree (which
    shares one object store and forbids the same branch in two places). The native
    worktree path stays **denied** (PreToolUse, ADR-0014); this package is the positive
    path the deny message points at.

The spawn flow — shipit owns spawning, the Tree is the Run substrate:
    The coordinator never hand-creates a Tree for a Run nor points an Agent tool at a
    checkout. It launches every real Run through ``shipit spawn subagent`` (ADR-0017;
    ``verbs/spawn.py``), which resolves the base, creates the Tree HERE (reusing
    ``tree.create`` — never reimplemented), and launches the backend agent as a **child
    process rooted in the Tree** (cwd = the Tree). Rooting the OS process — not a ``cd`` —
    is what defeats the bash-cwd footgun: the writes can't leak to the parent because the
    process itself lives in the Tree. The Run reports back **through the PR** (a writer
    opens a draft PR; a reviewer posts a review), so the coordinator drives it with the
    existing ``shipit pr status`` engine, never by scraping the child's stdout. The
    ``claude``-backend launch contract (headless ``claude -p --agent <role>``,
    ``ANTHROPIC_API_KEY`` scrubbed) is ADR-0019 / ``spawn/launch.py``. **Fail-closed**: a
    Tree-creation error fails the spawn loud — there is never a silent fallback to a
    native worktree.

The ``WorktreeCreate`` hook — the demoted adapter for in-CC spawns:
    An *in-session* ``Agent(isolation:"worktree")`` spawn fires Claude Code's
    ``WorktreeCreate`` hook (``verbs/hook/worktreecreate.py``). Instead of letting the
    harness mint a native worktree, the hook creates a Tree and prints its path, which
    Claude Code adopts as the subagent's cwd — so even the throwaway in-CC path lands in a
    real Tree, closing the #139 enforcement gap *by construction*. This is a **demoted
    convenience adapter**, not the spawn mechanism: it knows only the session-stable epic
    marker (not the per-spawn WS/role), so it builds a coarse ``<epic>/agent-<id>`` holding
    branch the spawned agent self-branches off, and it is **Claude-only**. Anything that
    needs a real branch-pinned Run, a non-Claude backend, or a PR-reported result goes
    through ``shipit spawn subagent``.

    The load-bearing mechanism — VERIFIED ``WorktreeCreate`` contract (live probe, Claude
    Code 2.1.196; pinned in ``verbs/hook/worktreecreate.py``): CC fires the hook with
    ``{session_id, transcript_path, cwd, prompt_id, hook_event_name, name}`` — the spawn-id
    field is **``name``** (value ``agent-<agentId>``), NOT ``worktree_name`` (an earlier
    guess that is always absent) — and CC then adopts the **bare path printed to stdout as
    the subagent cwd WITHOUT validating it**. That no-validation adoption is exactly what
    lets a dissociated clone path relocate the Run, with no cwd-footgun. NOTE the precise
    boundary: the footgun is eliminated only for the **rooted-process / hook-relocated**
    path (the OS process starts in the Tree). It does NOT fix an Agent tool merely *pointed*
    at an external path while its bash still resets to the parent — which is why that
    pointing pattern is rejected, not supported.

Two Tree modes (ADR-0018):
    - **Write Tree** (``tree.create``): one per write-Run — clone + ``.treeinclude`` + pixi
      provisioning + per-Tree ``target/`` & sccache (ADR-0015), read-write. Consumers:
      implementer, shepherd.
    - **Read-only Tree** (``tree.readonly``): clone + ``git checkout`` only — NO
      ``.treeinclude``, NO provisioning — then ``chmod``'d read-only. **Shared per
      ``(repo, branch)``**: N reviewers on one PR head share ONE cheap clone (safe because
      none mutate it). Consumer: reviewer Run. The real axis is **branch-pinned vs
      ambient**, not read vs write: an *ambient* explorer gets NO Tree (main checkout); a
      *branch-pinned* reviewer gets this cheap shared one.

Constraints & trade-offs:
    - **Local-only provisioning.** Trees live on one host; ``origin`` is the cross-machine
      sync point. No multi-machine Tree distribution, no container/runtime isolation —
      Trees give *file* isolation only (ADR-0017 leans on sccache to keep per-spawn Trees
      cheap, ~1.5s; the reflink warm-template of ADR-0015 stays the deferred escape hatch).
    - **The ``chmod`` on a read-only Tree is a guardrail, not a security boundary** — it
      catches an accidental write and keeps a shared clone trustworthy for co-tenants.
    - **The ``SHIPIT_EPIC`` marker is NOT an identity channel.** It survives only as the
      WorktreeCreate hook's optional tree-NAMESPACE override for the rare cross-epic
      in-CC spawn (``harness/worktree_adapter.py``; the hook normally infers the epic
      from the coordinator's branch prefix). The never-set "session marker" gap it once
      papered over is retired (ADR-0032 / LOG04-WS02): a worker's dev-cycle identity —
      ``epic``/``ws``/``agent``/``role`` — binds at the ``shipit spawn subagent`` seam
      from the spawn's own arguments and rides the Run's environment as
      ``SHIPIT_LOG_CTX_*`` (``shipit.logcontext``), so every shipit command the worker
      runs correlates to its Work Stream with zero worker cooperation.

ADR map: 0014 (dissociated clones / central root, native worktree denied), 0015 (per-Tree
``target/`` + cross-Tree sccache; reflink template deferred), 0016 (slash-namespaced
branches + ``EPIC/umbrella``), 0017 (shipit owns spawning; Tree as the Run substrate),
0018 (write vs read-only Trees), 0019 (the headless-``claude`` launch contract).

Package layout — the ``shipit tree create|list|remove|gc`` surface, split along the
``prstate`` "snapshot → decision" seam (pure planners/classifiers table-tested in
isolation, I/O kept thin at the edges):

- :mod:`shipit.tree.layout` — pure planning: ``plan(spec) -> TreePlan{dir, branch,
  base}`` for all three spec shapes (``--issue``, ``--epic E --ws N``, freeform
  ``--branch``). No I/O, table-tested.
- :mod:`shipit.tree.include` — pure matching: ``.treeinclude`` (``.gitignore``
  syntax) → the gitignored-but-needed files to copy into a fresh Tree.
- :mod:`shipit.tree.create` — the effectful orchestrator behind ``create``: clone
  (dissociated), fetch, checkout, apply ``.treeinclude``, provision (``shipit
  install`` + ``pixi``/``npm``) with the ADR-0015 build env, emit READY. The git
  boundary lives in :mod:`shipit.gh`; provisioning commands go through
  ``create.run_provision``. The write-Tree half of ADR-0018.
- :mod:`shipit.tree.readonly` — the read-only-Tree half of ADR-0018: clone +
  ``git checkout`` only (no ``.treeinclude``, no provisioning), files ``chmod``'d
  read-only and per-Run (ADR-0074): a reviewer clone dated by its own files.
- :mod:`shipit.tree.registry` — manifest-less fleet scan behind ``list``:
  ``scan(root) -> [TreeRecord]`` reads each clone's state straight off disk.
- :mod:`shipit.tree.activity` — the reclaim signal (ADR-0072):
  ``newest_mtime(path)`` measures when anyone last wrote a file in a Tree, over a
  walk with the build/env dirs pruned; unreadable answers ``None``.
- :mod:`shipit.tree.cleanup` — pure ``gc`` partition: ``classify(records, now) ->
  Cleanup`` splits the fleet into removable / keep on ONE rule for every kind —
  ``KEEP if dirty || unpushed || idle < 48h`` (ADR-0072).

The ``verbs/tree.py`` click group is the thin CLI over these.
"""

from __future__ import annotations
