"""Unit tests for `shipit.review.usage` — token-usage capture from CLI output
(RVW03-WS04, #667).

The parsers are pinned against the PROBED output shapes (2026-07-10): the
claude 2.1.206 result-envelope ``usage`` block, codex 0.139.0's stderr
``tokens used`` figure, and agy 1.1.1's nothing-at-all. The honesty rule under
test everywhere: a shape drift or an absent block degrades to the EXPLICIT
unknown (``total_tokens: None`` + ``source: "unreported"``), never a fabricated
number and never a zero.
"""

from __future__ import annotations

from shipit.review import usage

# --- the claude envelope (probed 2.1.206) ---------------------------------------


def test_claude_envelope_usage_totals_all_token_classes():
    # The probed envelope shape: input/output plus BOTH cache counters — every
    # token the run consumed folds into the total.
    envelope = {
        "result": "OK",
        "session_id": "sess",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 40,
            "cache_read_input_tokens": 17454,
            "cache_creation_input_tokens": 8270,
        },
    }
    parsed = usage.from_claude_envelope(envelope)
    assert parsed.total_tokens == 10 + 40 + 17454 + 8270
    assert parsed.input_tokens == 10
    assert parsed.output_tokens == 40
    assert parsed.source == usage.SOURCE_CLAUDE_ENVELOPE
    assert parsed.reported is True


def test_claude_envelope_without_cache_counters_still_totals():
    parsed = usage.from_claude_envelope(
        {"usage": {"input_tokens": 5, "output_tokens": 7}}
    )
    assert parsed.total_tokens == 12


def test_claude_envelope_missing_or_malformed_usage_is_unreported():
    assert usage.from_claude_envelope({"result": "OK"}) is usage.UNREPORTED
    assert usage.from_claude_envelope({"usage": "not-a-block"}) is usage.UNREPORTED
    # A usage block carrying no token counts at all is unknown, not zero.
    assert usage.from_claude_envelope({"usage": {"service_tier": "standard"}}) == (
        usage.UNREPORTED
    )


def test_claude_envelope_rejects_bools_and_negatives_as_counts():
    # JSON `true` is a Python bool (an int subclass) and a negative count is
    # nonsense — neither may masquerade as a measurement.
    parsed = usage.from_claude_envelope(
        {"usage": {"input_tokens": True, "output_tokens": -3}}
    )
    assert parsed is usage.UNREPORTED


# --- the codex stderr figure (probed 0.139.0) ------------------------------------


def test_codex_stderr_tokens_line_parses_the_comma_grouped_figure():
    stderr = (
        "OpenAI Codex v0.139.0\n--------\nreasoning effort: low\n"
        "codex\nOK\ntokens used\n11,943\n"
    )
    parsed = usage.from_codex_stderr(stderr)
    assert parsed.total_tokens == 11943
    assert parsed.source == usage.SOURCE_CODEX_STDERR
    # The coarse single figure cannot split input/output.
    assert parsed.input_tokens is None and parsed.output_tokens is None


def test_codex_stderr_tolerates_a_same_line_colon_rendering():
    # Tolerance for the same-line rendering so a minor CLI formatting change
    # does not silently zero the measurement.
    assert usage.from_codex_stderr("tokens used: 2,500\n").total_tokens == 2500


def test_codex_stderr_without_the_line_is_unreported_never_zero():
    assert usage.from_codex_stderr("codex: some log noise") is usage.UNREPORTED
    assert usage.from_codex_stderr("") is usage.UNREPORTED


# --- the record shape -------------------------------------------------------------


def test_as_record_is_the_round_runs_usage_shape():
    assert usage.UNREPORTED.as_record() == {
        "total_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "source": usage.SOURCE_UNREPORTED,
    }
    measured = usage.TokenUsage(total_tokens=42, source=usage.SOURCE_CODEX_STDERR)
    assert measured.as_record()["total_tokens"] == 42
    assert measured.as_record()["source"] == "codex-stderr"
    assert measured.reported is True
