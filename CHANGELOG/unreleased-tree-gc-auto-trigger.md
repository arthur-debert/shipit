- `tree gc` now **runs automatically** — the SessionStart hook fires a debounced,
  detached fleet sweep, so stale Trees are reclaimed continuously instead of
  piling up until a human remembers to sweep by hand (#1011, ADR-0072). gc had
  exactly one caller, the manual verb, which is how 526 stale Trees once
  accumulated. Now that a sweep streams its removals, exits loud on a partly-seen
  fleet, and costs ~one `gh` call per repo, it is safe to automate. The trigger
  lives at the SessionStart boundary (already the Tree-lifecycle/liveness seam);
  the sweep is spawned DETACHED so it never sits on the session-start latency
  path; and a stamp at the central root debounces it to **~one sweep per 30
  minutes** — touched before the spawn so the herd of concurrent session starts
  (every session, coordinator and subagent, fires the hook) collapses to a single
  sweep per window. The trigger is fail-open: any error costs the session nothing
  and the manual `shipit tree gc` verb is unchanged.
