# Severity-tier dimension set: experiment-only arm (amends ADR-0045)

ADR-0045 rejected severity-scoped finders ("a highs-only agent") on the
grounds that the 2025-26 evidence backed dimension-scoping and pass
aggregation but did NOT back severity-scoping — an absence-of-evidence
rejection made before the Review Lab existed. The broader literature's
strongest configuration is precisely a severity-tier fan-out (blocking
defects / design-robustness-tests / polish), and the Lab (ADR-0048,
ADR-0049) now exists to settle exactly this kind of judgment call with a
measured convergence curve instead of priors.

Decision: the closed dimension registry gains an EXPERIMENT-ONLY
severity-tier set — `sev-critical-high`, `sev-medium`, `sev-low` — selectable
solely via an explicit `dimensions` list (a Lab cell or a Roster override).
The shipped default set is UNCHANGED: the ADR-0045 concern-scoped four
remain the production round-1 decomposition, and severity continues to be
assigned at calibration for that default path. ADR-0045's rejection is
narrowed, not reversed: it now reads "not in the shipped default without
Lab evidence" rather than "do not add one here".

The deciding instrument is the `fanout-sevtiers` cell (control:
`fanout-baseline`, axis: pass scoping — severity tiers vs concern
dimensions, fixture v36). Revisit the shipped default only when that curve
(or a successor cell) delivers a major-or-worse recall / equal-budget
verdict; adopting the tiers as default would be its own ADR.

Consequences: the registry stays closed (+3 entries); pass prompts for the
tier set bound the severities a pass may emit, which the concern set never
did; the tier prompts are experiment material — editing their focus text
changes the recorded instructions-variant hash and orphans banked lab
points, so wording changes after a run mean a deliberate re-run.
