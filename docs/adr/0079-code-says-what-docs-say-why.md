# Code says what; docs say why

> **Status: Accepted.** Epic DOC01 (#1144). **Amends** the docstring-maintenance
> ground rule in `src/shipit/data/roles/_base.lex` (the same-diff sweep of
> caller and module docstrings) and extends the reviewer's style clause.
> **Instantiates ADR-0036** (the gate owns style): the cap and the
> reference ban become configured lint rules, because a standard is
> machine-enforced or it does not exist.

Code prose states **what** a thing does, plus any contract a caller cannot infer
from the name and the signature. It does not state **why** the design is the way
it is.

## Context

`src/` carries **35,053 lines of prose** (27,153 docstring, 7,900 comment)
against **38,104 lines of code** — a 0.92:1 ratio. **191 of 210 files** embed
`ADR-NNNN` references (1,683 of them) and `#NNN` issue references (1,258 of
them). `git.py` is the emblem: 425 lines of code carrying 918 lines of prose to
describe a git adapter doing obvious git things.

That is not a tidiness problem, it is a velocity problem, and the mechanism is
duplication. The same rationale is restated at every site that touches the
decision, so N copies drift out of step with the one place that is actually
authoritative. Two costs fall out and both are paid on every change this repo
makes:

- **Every code change becomes a multi-file prose edit.** The ground rule this
  ADR amends told each implementer to update the changed function's docstring
  *plus* the module docstring *plus* every caller's docstring in the same diff.
  A one-line behavioural change therefore lands as a four-file diff, three
  quarters of which is prose.
- **Docstring drift became a recurring review finding.** Prose that restates a
  decision goes stale the moment the decision moves; reviewers then spend rounds
  on the copies rather than on the code.

The repo already has homes for every one of those ideas — 80 ADRs, a `CONTEXT.md`
glossary, `docs/dev/*.lex`, git, and the tracker. The duplication buys nothing
those homes do not already provide, better.

## Decision

**Each idea gets exactly ONE canonical home.**

| Kind of knowledge | Home |
| --- | --- |
| What a thing does; a contract a caller cannot infer | the code, terse |
| Why the design is this way; alternatives rejected | `docs/adr/` |
| Vocabulary — what a term means in this domain | `CONTEXT.md` |
| Process — how we work, how a verb is operated | `docs/dev/` |
| History — what changed, when, and under which issue | git and the tracker |

Concretely, in `src/`:

- **A docstring states the contract, not the argument for it.** Behaviour,
  arguments, return, raises, and any invariant a caller would otherwise have to
  read the body to learn. Never the rationale, the alternatives, the measurement
  that motivated it, or the story of how it came to be.
- **Terse is correct.** A function whose name and signature already say what it
  does needs no docstring at all. A one-line docstring is a complete docstring.
- **ONE ADR pointer, one place.** A module that directly implements a single ADR
  may carry ONE `See docs/adr/NNNN-slug.md` pointer in its **module** docstring.
  No other ADR reference and no issue reference appears anywhere in `src/` —
  not in function docstrings, not in comments, not as an epic code.
- **No history in code prose.** "was X, now Y", "since #NNN", "renamed from",
  "the old behaviour" — git holds that, and holds it accurately.

The target for the epic that lands this is `src/` prose of **1,000–2,500 lines**.
The rationale is not destroyed; it is relocated to the home it already had.

## Considered options

- **Leave it to the prompts.** Rejected — the prompts are exactly how this grew.
  The amended ground rule reduces the *instruction* to write prose, but ADR-0036
  is explicit that an unenforced standard drifts. Prompt text alone produced a
  0.92:1 ratio; there is no reason to expect the next prompt to hold better.
- **Ban docstrings below some size, or mandate a house style (D4xx).** Rejected
  as the same mistake mirrored: it re-introduces a prose ratchet, just pointed
  the other way, and ADR-0036 already ruled strict docstring enforcement out of
  scope.
- **Keep the ADR/issue references as navigation.** Rejected on the measurement:
  2,941 references across 191 files is not navigation, it is a second index that
  nobody regenerates. An ADR knows which module implements it; the module does
  not need to know the reverse 1,683 times over.

## Consequences

- **The `_base.lex` ground rule is replaced.** The duty shrinks to: keep the
  one-line contract of what you changed accurate. No caller sweep, no sibling
  modules, no module-docstring ritual unless the change actually invalidates a
  sentence written there. The true half survives — a docstring that contradicts
  the code is the code lying to the next reader.
- **The reviewer clause cuts both ways.** ADR-0036 already barred a reviewer
  from demanding *more* style; asking an author to expand a docstring was still
  a live move. It is now explicitly not a finding, and prose that adds
  rationale, references, or narrative to code IS one.
- **The cap becomes a lint rule** (DOC01-WS15): a configured `shipit lint` check
  capping docstring length and rejecting `ADR-` / `#NNN` references in code
  prose. It lands after the cleanup slices, so it is green on arrival. Until
  that rule exists, this ADR is a decision and not yet a gate.
- **Deleting 33k lines of prose must be proven inert.** Each cleanup slice
  carries an AST-equivalence run — parse before and after, strip docstrings,
  compare executable ASTs — plus green `pixi run test`. Line-by-line human
  review of a 30k-line deletion would be the same excess in a new costume.
- **One known coupling stays.** 59 Click-decorated functions render their
  docstring as `--help` output, asserted by `tests/test_cli.py`; those keep
  exactly one line.
- **`tests/`, `docs/`, and `src/shipit/data/` are out of scope.** Test prose
  runs at a 0.10 ratio and is doing its job; `docs/` and the shipped data
  surfaces are where the why is supposed to live.
