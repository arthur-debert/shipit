## Development workflow (managed by shipit)

This project follows the portfolio dev cycle. The lines below are shipit-managed;
edit the surrounding `AGENTS.md`, not this block — `shipit install` regenerates it.

### The PR lifecycle

Every change ships as a PR the agent drives. Open it as a DRAFT — a draft is WIP
the agent owns. Shepherd the whole loop while it stays draft: request and address
reviews, get CI green, and make it mergeable. Flipping draft → ready is the ONE
signal that means "done iterating — a human can validate and merge", so it happens
only when all three hold: reviews addressed, CI green, mergeable.

Stop at that flip; do NOT merge. Opening as a draft and flipping it to ready is the
agent's job; the human does the final read and merge unless they ask otherwise. A
human request for changes flips the PR back to draft; the loop repeats and re-flips
to ready when green.

### Addressing reviews

The local agent has more context than the reviewing agent, so it has the final
word. A review comment is either addressed in a commit or answered with a rationale
for the pushback, and every comment is marked resolved so the review stays readable.
Push all commits for a round at once, so re-run-on-push reviewers fire only once.
