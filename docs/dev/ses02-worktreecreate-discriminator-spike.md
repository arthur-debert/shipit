# SES02-WS01 spike — the WorktreeCreate coordinator-vs-helper discriminator

**Question.** `claude --worktree <id>` (the coordinator's own launch) and an in-CC
`Agent(isolation:"worktree")` helper spawn fire the SAME `WorktreeCreate` hook. The
hook must fork — coordinator → the ephemeral session Tree (`ephemeral/<id>` off
`origin/main`, ADR-0027); helper → the existing `<epic>/agent-<id>` holding branch
(ADR-0017). What in the payload tells them apart?

**Verdict: `prompt_id` — absent ⇒ coordinator launch.** Confirmed by live probe,
Claude Code **2.1.198** (2026-07-02). Implemented as
`shipit.harness.worktree_adapter.is_coordinator_launch`.

## Method

A throwaway git repo whose `.claude/settings.json` wires `WorktreeCreate` to a
capture script (`cat > /tmp/…/payload-<ns>.json`, then print a valid dir so the
launch/spawn proceeds). Two probes against it:

1. `claude --worktree spike-coordinator -p 'Reply with exactly: hi'` — the
   top-level launch.
2. `claude -p 'Use the Agent tool … isolation "worktree" …' --allowedTools Agent`
   — an in-CC helper spawn (headless, so the field set is the headless one too).

## Evidence — the captured payloads, verbatim

Probe 1, top-level `--worktree` launch (**no `prompt_id`**; `name` = the
`--worktree` value verbatim):

```json
{
    "session_id": "c6010bf9-c522-4af7-9c50-3cac4ba8ca21",
    "transcript_path": "/Users/adebert/.claude/projects/-private-tmp-ses02-spike-repo/c6010bf9-c522-4af7-9c50-3cac4ba8ca21.jsonl",
    "cwd": "/private/tmp/ses02-spike/repo",
    "hook_event_name": "WorktreeCreate",
    "name": "spike-coordinator"
}
```

Probe 2, in-CC `Agent(isolation:"worktree")` spawn (**carries `prompt_id`**;
`name` = `agent-<agentId>`):

```json
{
    "session_id": "571d0dfe-4f05-4c7f-a8c1-a8593c5acabf",
    "transcript_path": "/Users/adebert/.claude/projects/-private-tmp-ses02-spike-repo/571d0dfe-4f05-4c7f-a8c1-a8593c5acabf.jsonl",
    "cwd": "/private/tmp/ses02-spike/repo",
    "prompt_id": "c2f52d57-6eb7-469b-b8ef-3001e450ecaf",
    "hook_event_name": "WorktreeCreate",
    "name": "agent-ac36b2efb04c97d80"
}
```

This matches the earlier probes on both sides: the 2.1.196 in-CC contract pinned in
`verbs/hook/worktreecreate.py` (payload WITH `prompt_id`) and the ADR-0027 2.1.198
`--worktree` spike (payload WITHOUT it).

## Why `prompt_id` is the right signal (and the name prefix is not)

- **It is structural.** A top-level `--worktree` launch runs the hook during
  process startup — there IS no prompt yet, so CC has no `prompt_id` to send. An
  in-CC spawn is always triggered by a turn, so it always carries one. The
  distinction is inherent to *when* the hook fires, not a formatting convention.
- **The name convention corroborates but cannot be trusted.** Helper spawns are
  always `name: agent-<agentId>` (CC's own throwaway id) and `agent-start claude`
  mints `sess-…` ids — but `name` for the coordinator is whatever the user passed to
  `-w`, including, pathologically, something starting with `agent-`. The
  `sess-` prefix stays a *recognizability* convention (and the documented fallback
  discriminator if a future CC version ever adds `prompt_id` to the launch
  payload), not the mechanism.
- **Misclassification degrades safely both ways.** Either caller still lands in a
  real, provisioned, dissociated Tree (never a native worktree — the #139
  invariant holds by construction); a wrong fork only picks the other branch
  namespace.
