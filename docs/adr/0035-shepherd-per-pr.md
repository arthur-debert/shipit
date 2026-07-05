# Shepherd-per-PR, round-per-message

> **Status: Accepted.** Epic RVW02 (#453); decided in the ADP00 retrospective.
> Reverses the fresh-shepherd-per-round design encoded in the shepherd and
> coordinator roles and `docs/dev/epics.md` since RVW (#424). The engine's
> authority (ADR-0031) and the classification seam (#423) are unchanged.

One shepherd owns ADDRESSING for a PR across its whole review life. It is
briefed cold once (round 1), then PARKED between rounds — doing nothing — and
resumed with a one-line message when the coordinator's wait (ADR-0034's
`pr wait`) reports the next round in. The role's other boundaries stand
untouched: a shepherd never waits, never flips to ready, never coordinates,
and classifies every finding through `pr classify` exactly as before. The
coordinator still owns every wait and the draft→ready flip.

The measured basis. A cold shepherd spends roughly 40k tokens and two minutes
re-deriving PR context before its first action; ADP00 ran ~19 addressing
rounds across 16 PRs, so the per-round rebirth cost roughly half a million
tokens and three-quarters of an hour of pure re-orientation — paid even for
rounds whose entire diff was four characters. The pattern that replaces it was
proven in the same epic: the canary coordinator ran as one persistent agent
re-briefed per round across seven rounds, and its held context (its own prior
findings, preserved evidence) made each successive round cheaper and sharper,
not sloppier.

The trade-off is named, not waved off: a persistent shepherd can anchor on its
own round-1 rationale, and fresh-per-round bought guaranteed-zero context
bleed. Mitigations: the resume brief restates the ENGINE's verdict for the new
round (the shepherd re-reads findings from the PR, not from memory);
`pr classify` verdicts are write-once, so an anchored re-litigation cannot
silently overwrite an earlier call; and fresh-per-round remains a permitted
fallback at the coordinator's discretion when a shepherd's context is judged
compromised — the default flips, the option survives.

While in there, the shepherd role gains the root-cause sweep clause (the
whack-a-mole lesson from ADP00's canary, applied at PR scale): a valid finding
is usually an instance of a CLASS, so before pushing, sweep the whole PR diff
for other instances of that class — the same missing convention, the same
stale cross-reference, the same escaping bug — and fix them in the same round,
rather than letting each instance buy the reviewers another round.

Considered and rejected: shepherd-owns-drive-to-READY (the shepherd would idle
through waits, either burning turns polling or duplicating the suspension the
coordinator already gets for free behind `pr wait`; and it splits "who owns
the wait" across two roles); keeping fresh-per-round (a measured recurring
cost purchasing a hygiene benefit the resume-brief discipline provides more
cheaply); pooling one shepherd across PRs (crosses the one-writer-per-Tree
boundary and mixes PR contexts — the bleed risk fresh-per-round existed to
kill, with none of its guarantees).

Consequences: shepherd and coordinator `.lex` sources plus `docs/dev/epics.md`
are rewritten (RVW02-WS02); a multi-round PR costs one warm-up total instead
of one per round; the coordinator's dispatch loop becomes wait → resume → wait;
and round-2+ economics become observable in the flow log (same agent id across
rounds) instead of vanishing into per-spawn accounting.
