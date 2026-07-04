"""The finding-verdict store (#423): the dev-cycle event log as the record.

One `finding.classified` event per verdict — keyed by the finding comment's
id, written once, immutable — read back as `comment id -> verdict` for the
snapshot (`ReadinessView.verdicts`). No auto-classification exists anywhere:
these tests pin the store's two halves (load/record), the write-once refusal,
and the closed verdict vocabulary.
"""

from __future__ import annotations

import json
import logging

import pytest

from shipit import events
from shipit.identity import repo_from_slug
from shipit.prstate.errors import PrStateError
from shipit.prstate.verdicts import (
    NITPICK,
    SUBSTANTIVE,
    VERDICT_EVENT,
    VERDICTS,
    load_verdicts,
    record_verdict,
)

REPO = repo_from_slug("owner/repo")


def verdict_record(pr: int, comment: int, verdict: str, **extra) -> str:
    """One JSONL line as the logging pipeline lands it (flat fields, ADR-0029)."""
    return json.dumps(
        {
            "event": VERDICT_EVENT,
            "pr": pr,
            "comment": comment,
            "verdict": verdict,
            "msg": f"finding {comment} on pr#{pr} classified {verdict}",
            **extra,
        }
    )


def write_log(base, lines, name="shipit.log"):
    log_dir = base / "owner" / "repo"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_vocabulary_is_closed_and_registered():
    assert VERDICTS == (NITPICK, SUBSTANTIVE) == ("nitpick", "substantive")
    # The event rides the ADR-0032 registry — the reader/writer contract.
    assert VERDICT_EVENT in events.EVENT_NAMES


def test_load_folds_the_pr_scoped_verdicts(tmp_path):
    write_log(
        tmp_path,
        [
            verdict_record(5, 100, "nitpick"),
            verdict_record(5, 200, "substantive", reason="changes behaviour"),
            verdict_record(7, 300, "nitpick"),  # another PR — not ours
            json.dumps({"msg": "an ordinary non-event record", "pr": 5}),
            "not json at all",  # a torn write cannot poison the fold
        ],
    )
    assert load_verdicts(REPO, 5, base_dir=tmp_path) == {
        100: "nitpick",
        200: "substantive",
    }
    assert load_verdicts(REPO, 7, base_dir=tmp_path) == {300: "nitpick"}


def test_load_missing_file_is_no_verdicts(tmp_path):
    assert load_verdicts(REPO, 5, base_dir=tmp_path) == {}


def test_load_ignores_malformed_verdict_fields(tmp_path):
    write_log(
        tmp_path,
        [
            verdict_record(5, 100, "cosmetic"),  # out-of-vocabulary verdict
            json.dumps(
                {
                    "event": VERDICT_EVENT,
                    "pr": 5,
                    "comment": "100",
                    "verdict": "nitpick",
                }
            ),  # str id
            json.dumps(
                {"event": VERDICT_EVENT, "pr": 5, "verdict": "nitpick"}
            ),  # no id
        ],
    )
    assert load_verdicts(REPO, 5, base_dir=tmp_path) == {}


def test_load_reads_rotated_backups_and_first_write_wins(tmp_path):
    # The writer is a RotatingFileHandler: an older verdict can live in a
    # backup. And should a duplicate ever exist despite the write guard, the
    # FIRST (oldest) record stays authoritative — verdicts are immutable.
    write_log(tmp_path, [verdict_record(5, 100, "nitpick")], name="shipit.log.1")
    write_log(tmp_path, [verdict_record(5, 100, "substantive")])
    assert load_verdicts(REPO, 5, base_dir=tmp_path) == {100: "nitpick"}


def test_record_emits_the_registered_event_with_flat_identity(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        record_verdict(REPO, 5, 123, NITPICK, reason="wording only", base_dir=tmp_path)
    (rec,) = [
        r for r in caplog.records if getattr(r, events.EXTRA_KEY, None) == VERDICT_EVENT
    ]
    assert rec.pr == 5
    assert rec.comment == 123
    assert rec.verdict == "nitpick"
    assert rec.reason == "wording only"


def test_record_refuses_a_reclassification(tmp_path):
    # Write-once: the verdict is immutable — re-classifying an
    # already-classified comment is an error, in BOTH directions.
    write_log(tmp_path, [verdict_record(5, 123, "nitpick")])
    with pytest.raises(PrStateError, match="already classified 'nitpick'"):
        record_verdict(REPO, 5, 123, SUBSTANTIVE, base_dir=tmp_path)
    with pytest.raises(PrStateError, match="written once"):
        record_verdict(REPO, 5, 123, NITPICK, base_dir=tmp_path)


def test_record_refuses_an_unknown_verdict(tmp_path):
    with pytest.raises(PrStateError, match="unknown verdict"):
        record_verdict(REPO, 5, 123, "cosmetic", base_dir=tmp_path)


def test_record_scopes_the_write_once_guard_per_pr(tmp_path, caplog):
    # The same comment id on a DIFFERENT PR is a different finding (comment ids
    # are globally unique on GitHub, but the guard keys the record it reads:
    # per-PR) — recording it must not trip the other PR's verdict.
    write_log(tmp_path, [verdict_record(5, 123, "nitpick")])
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        record_verdict(REPO, 9, 123, SUBSTANTIVE, base_dir=tmp_path)
    assert any(
        getattr(r, events.EXTRA_KEY, None) == VERDICT_EVENT for r in caplog.records
    )
