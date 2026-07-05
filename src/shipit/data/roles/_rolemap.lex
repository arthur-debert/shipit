Role map

The roles a coordinator delegates to — one line each. The binding prompt for
each subagent role lives in its agent-def under `.claude/agents/`:

- implementer — builds the change with tests and opens the draft PR, then stops.
- shepherd — owns addressing for one PR across its review rounds; parked between rounds, resumed per round.
- explorer — read-only investigator: searches and reports, changes nothing.
- reviewer — read-only, branch-pinned: reads a PR head and posts one review, changes nothing.
