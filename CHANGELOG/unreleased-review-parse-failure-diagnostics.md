- review: parse-failure diagnostics are evidence-based — only an explicit
  backend timeout recommends a faster model or a smaller diff (#1006, #1033
  as context). The old catch-all reported every unparseable reviewer response
  as "no parseable JSON … try a faster model or a smaller diff", which was
  actively wrong when `agy`'s model narrated prose instead of answering on a
  4-file docs diff (the pin itself was reverted by #1032). `parse_review_output`
  now distinguishes what the raw output actually shows: an explicit timeout
  marker (the one case where size/latency advice is honest), empty stdout (a
  silent non-delivery), complete JSON with the wrong `{summary, comments}`
  envelope (an output-contract fault, #826), and everything else — prose,
  narration, partial JSON — as a conservative "no review verdict" that points
  at the raw output instead of guessing a cause. Raw-output salvage (#76) and
  the structured `timed_out` flag are unchanged; no static model blacklist is
  introduced — AGY reviewer health is follow-up runtime-provenance work, and
  this change does not claim it fixed.
