# Ground-truth fixture with banked Adjudication; the scorer is deterministic

Review quality was measured by humans reading eval reports against three
historically-known majors — small enough that one finding swung recall by 33
points (RVW02 WS05/#638, #665), and no false-positive rate existed at all. We
decided review measurement runs against a **versioned, in-repo ground-truth
fixture**: a corpus of pinned historical PR ranges whose labels carry provenance
(a fix commit, a maintainer-confirmed thread, or a banked Adjudication), scored
by a **fully deterministic scorer** — same file, line within the label's range,
normalized claim-token overlap. No LLM is ever part of the measuring
instrument: a misjudging semantic matcher is the RVW02 calibrator failure
reproduced one level up, in the ruler itself. An LLM matcher may be a *cell
under test*, never the scorer.

Labels are admitted on evidence, not opinion: fixture v1 targets 8–12 portfolio
PRs and ≥25 major-or-worse ground-truth positives (spanning language, diff
size, and defect character); any severity tier with fewer than ~20 positives is
rendered with an **underpowered** marker, never as a headline number. Purely
synthetic bug-injection is excluded from the fixture core — it measures "finds
planted bugs," which the RVW02 literature review found diverges from real
recall.

The fixture grows as a side effect of running experiments: an emitted finding
that matches no label is **adjudicated once** (agent proposes, human confirms)
and the verdict — real or not-real — is banked as a new label; a near-miss
(right file, overlapping lines, claim below the lexical threshold) is surfaced,
adjudicated, and banked as a phrasing **alias** on its label. The scorer stays
deterministic and free to re-run forever; the fixture absorbs the semantics
over time. This supersedes the retired `finding.classified` verdict log (#668)
as the home of verdicts, and every scored result records the fixture version it
ran against — numbers scored against different fixture versions are never
comparable. The matching primitive (file + line-proximity + claim-overlap +
aliases) is shared with semantic dedup of same-round findings (#673): one
definition of "the same claim," tuned in one place.
