# adebert-skills

Portable, agent-agnostic Skills, distributed as plain `SKILL.md` folders (no
Claude-Code plugin/marketplace wrapper, so they work with any agent that reads
the `SKILL.md` convention).

Each skill folder lives under `skills/` and is self-contained. Names carry an
`adebert-` prefix so they never clash with the upstream third-party skills they
were forked from.

## Skills

| Skill | Forked from | What changed |
|-------|-------------|--------------|
| `adebert-to-prd` | [mattpocock/skills](https://github.com/mattpocock/skills) `to-prd` | PRD is written as a file in `docs/prd/` (single source of truth); the epic tracker issue links to it instead of embedding the body. |
| `adebert-to-issues` | [mattpocock/skills](https://github.com/mattpocock/skills) `to-issues` | Vertical slices are framed as **Work Streams**; dropped the max-thinness bias (each WS = thinnest *coherent, reviewable* PR) and the HITL/AFK tagging (all AFK); kept the blocked-by dependency graph; WS may overlap files. |

## Install

These are just folders — point your agent's skills directory at them.

**Symlink an individual skill** into a project (or your user-level skills dir):

```sh
ln -s ~/h/adebert-skills/skills/adebert-to-prd    <repo>/.claude/skills/adebert-to-prd
ln -s ~/h/adebert-skills/skills/adebert-to-issues <repo>/.claude/skills/adebert-to-issues
```

(For other agents, symlink/copy into whatever directory that agent scans for
`SKILL.md` folders.)

## Notes

- These skills reference `/grill-with-docs` (upstream, unchanged) and
  `/setup-matt-pocock-skills` (issue-tracker + label vocabulary). Install those
  separately if your flow uses them.
- The surrounding workflow (epic branches, the coordinating-agent execution
  model, branch/issue naming, the bug track, review-round breaking) lives in the
  user's global instructions, not in these skills — these only correct the two
  points where the upstream skills contradicted that workflow.
