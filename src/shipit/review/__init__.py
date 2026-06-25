"""review — local code-review backends (PRF01-WS07).

A generic, SINGLE-repo, single-PR review model: resolve a PR to its unified diff
(:func:`shipit.review.diff.resolve_pr`), build one shared prompt body from review
instructions + that diff, hand it to a pluggable agent backend (codex / agy),
parse the agent's JSON verdict, and post it back to the PR AS the agent's GitHub
App identity (``adr-codex-review[bot]`` / ``adr-agy-review[bot]``).

Ported from release-core's ``review`` package. The one deliberate divergence:
App auth (:mod:`shipit.review.ghauth`) sources the App private key + app id from
Doppler via :mod:`shipit.secretsrc` (in-memory PEM, never disk), replacing
release's ``~/.config/release-review/apps/*.pem`` disk lookups.

**One-way edge:** ``prstate.reviewers`` lazy-imports this package; this package
NEVER imports ``prstate``. The reviewer adapters call into
:mod:`shipit.review.service` to run + post a local review synchronously.
"""

from __future__ import annotations
