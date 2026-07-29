from __future__ import annotations

import pytest

from shipit.review import usage


def test_claude_envelope_usage_totals_all_token_classes():
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


def test_claude_envelope_zero_tokens_is_a_real_measurement_not_degraded():
    parsed = usage.from_claude_envelope(
        {"usage": {"input_tokens": 0, "output_tokens": 0}}
    )
    assert parsed.total_tokens == 0
    assert parsed.reported is True
    assert parsed is not usage.UNREPORTED


def test_claude_envelope_missing_or_malformed_usage_is_unreported():
    assert usage.from_claude_envelope({"result": "OK"}) is usage.UNREPORTED
    assert usage.from_claude_envelope({"usage": "not-a-block"}) is usage.UNREPORTED
    assert usage.from_claude_envelope({"usage": {"service_tier": "standard"}}) == (
        usage.UNREPORTED
    )


@pytest.mark.parametrize(
    "block",
    [
        {"input_tokens": True, "output_tokens": -3},
        {"input_tokens": 10, "output_tokens": -3},
        {"input_tokens": True, "output_tokens": 7},
        {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": -1},
    ],
)
def test_claude_envelope_corrupt_count_poisons_whole_block(block):
    assert usage.from_claude_envelope({"usage": block}) is usage.UNREPORTED


@pytest.mark.parametrize(
    "block",
    [
        {"input_tokens": 5},
        {"output_tokens": 7},
        {"input_tokens": 5, "cache_read_input_tokens": 3},
    ],
)
def test_claude_envelope_requires_both_input_and_output(block):
    assert usage.from_claude_envelope({"usage": block}) is usage.UNREPORTED


def test_codex_stderr_tokens_line_parses_the_comma_grouped_figure():
    stderr = (
        "OpenAI Codex v0.139.0\n--------\nreasoning effort: low\n"
        "codex\nOK\ntokens used\n11,943\n"
    )
    parsed = usage.from_codex_stderr(stderr)
    assert parsed.total_tokens == 11943
    assert parsed.source == usage.SOURCE_CODEX_STDERR
    assert parsed.input_tokens is None and parsed.output_tokens is None


def test_codex_stderr_tolerates_a_same_line_colon_rendering():
    assert usage.from_codex_stderr("tokens used: 2,500\n").total_tokens == 2500


def test_codex_stderr_without_the_line_is_unreported_never_zero():
    assert usage.from_codex_stderr("codex: some log noise") is usage.UNREPORTED
    assert usage.from_codex_stderr("") is usage.UNREPORTED
    assert usage.from_codex_stderr(None) is usage.UNREPORTED


def test_codex_stderr_commas_only_figure_degrades_never_crashes():
    assert usage.from_codex_stderr("tokens used\n,,,\n") is usage.UNREPORTED


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
