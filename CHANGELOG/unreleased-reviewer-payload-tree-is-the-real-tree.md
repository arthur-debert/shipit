- spawn: the reviewer SPAWNED payload's `tree` now reports the reviewer's ACTUAL
  per-Run read-only Tree, not a speculative coordinate (#1039). ADR-0074 made
  review Trees per-Run with a minted UUID, so the flat-leaf naming
  `_launch_reviewer` reported and the UUID `review/producer.provision_review_tree`
  minted independently could no longer agree by computation — `payload["tree"]`
  named a plausible path the reviewer never ran in. The spawn boundary now mints
  the flat-leaf naming ONCE and threads it down through the review service
  (`run_detached_review` → `generate_review` → `run_fanout_review` →
  `provision_review_tree`) via a new optional `review_tree_naming` /
  `naming` parameter (default `None` = "mint your own", so the review adapters'
  own re-review path and every other caller are unchanged), so the producer clones
  the reviewer under that exact id. Two reviewers on the same head still mint
  distinct namings upstream, so their per-Run Trees — and payloads — still differ.
