"""``shipit.session`` — the coordinator session's own lifecycle concerns.

Everything about the top-level Claude Code session AS a process — its current-Tree
resolution (:mod:`shipit.session.current`), bootstrap, and resume — as opposed to
:mod:`shipit.tree` (where a session works) and :mod:`shipit.spawn` (the Runs a session
launches). Session liveness once lived here as a pidfile the ephemeral-Tree gc ladder
read; ADR-0072 replaced that ladder with activity-based reclaim, so the liveness module
retired with it.
"""
