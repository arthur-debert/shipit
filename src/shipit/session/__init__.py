"""``shipit.session`` — the coordinator session's own lifecycle concerns.

Everything about the top-level Claude Code session AS a process — today its
liveness (:mod:`shipit.session.liveness`, the pidfile + ``is_live`` seam the
ephemeral-Tree gc ladder reads) — as opposed to :mod:`shipit.tree` (where a
session works) and :mod:`shipit.spawn` (the Runs a session launches).
"""
