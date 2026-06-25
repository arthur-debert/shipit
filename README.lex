shipit

A small set of utilities and agent harnesses to standardize work across my portfolio
of personal projects (arthur-debert, lex-fmt and phos-editor on gh)

Scope

    1. Development Workflow: 
        1.1 The how to full for development workflow.
        1.1 The skills for development ()
the `SKILL.md` convention).

Each skill folder lives under `skills/` and is self-contained. Names carry an
`shipit-` prefix so they never clash with the upstream third-party skills they
were forked from.

## Skills

| Skill | Forked from | What changed |
|-------|-------------|--------------|
| `shipit-to-PRD` | [mattpocock/skills](https://github.com/mattpocock/skills) `to-PRD` | PRD is written as a file in `docs/PRD/` (single source of truth); the epic tracker issue links to it instead of embedding the body. |
| `shipit-to-issues` | [mattpocock/skills](https://github.com/mattpocock/skills) `to-issues` | Vertical slices are framed as **Work Streams**; dropped the max-thinness bias (each WS = thinnest *coherent, reviewable* PR) and the HILT/AFC tagging (all AFC); kept the blocked-by dependency graph; WS may overlap files. |

## Install

These are just folders — point your agent's skills directory at them.

**Symlink an individual skill** into a project (or your user-level skills dir):

```sh
Ln -s ~/h/shipit-skills/skills/shipit-to-PRD    <repo>/.claude/skills/shipit-to-PRD
Ln -s ~/h/shipit-skills/skills/shipit-to-issues <repo>/.claude/skills/shipit-to-issues
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
