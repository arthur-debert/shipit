- The **`agy` local reviewer works again** (#1006). It has been pinned to Gemini
  3.5 Flash since #990, and Flash goes *agentic* in `agy`'s headless `--print`
  mode: instead of reviewing the diff it is handed, it narrates its hunt for one
  and never emits a verdict. Every `agy-local` run therefore settled `failed`.
  The reviewer was not slow or wrong — it was **absent**, and had been for days.
  What made it invisible is worth recording, because nothing here misbehaved: a
  required reviewer that fails is *degraded*, and the PR engine deliberately
  declines to let a degraded reviewer block Ready — otherwise one broken
  reviewer would wedge every PR in the repo. So PRs kept flowing, green, with
  codex and Copilot passing, while the roster promised three required reviewers
  and delivered two. Measured on this repo: `agy-local` failed on **every PR of
  the TREE03 epic**, roughly ten review rounds, without ever once blocking one.
  A check that fails loudly on every run reads, over time, as furniture.
  `agy` returns to `pro` (Gemini 3.1 Pro (High)). The ~20% review-speed win that
  #989's spike measured for Flash is given up **deliberately**: a reviewer that
  never returns a verdict is not faster than one that does, it is not a reviewer.
