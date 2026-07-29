- docs: `src/` prose cut from **35,053 lines to 2,847** (91.9%), against 38,095
  lines of code — a prose-to-code ratio of 0.075:1, down from 0.920:1. Docstrings
  and comments now state **what** the code does and any contract a caller cannot
  infer from the name and signature; they no longer restate **why** a design is
  the way it is. 1,683 `ADR-NNNN` references and 1,258 `#NNN` issue references
  are gone from code prose, replaced by at most one `See docs/adr/…` pointer in
  the module docstring of a module that directly implements an ADR.
- The rationale was not deleted, it was left where it already lived: `docs/adr/`,
  `CONTEXT.md`, `docs/dev/`, git, and the tracker. Duplicating it across every
  call site meant N copies drifting out of step with the one authoritative home,
  and made every one-line code change a multi-file prose edit.
- **ADR-0079** records the decision, and the implementer role prompt no longer
  asks for the same-diff sweep of caller and module docstrings that produced the
  growth: the duty is now the one-line contract of what you changed. The reviewer
  prompt's style clause cuts both ways — a terse or absent docstring is never a
  finding, added rationale is one, and a docstring that *contradicts* its code
  remains a correctness finding at its own severity.
- Zero executable change: every one of the 207 touched files was verified by
  parsing before and after, stripping all docstrings, and comparing executable
  ASTs. Click command docstrings, which render as `--help` output, were preserved
  throughout.
