"""Behavioural tests for the central redactor (LOG01-WS02, ADR-0028/0029).

Three seams, each isolated:

- the redactor itself — registered-value masking, pattern masking, non-secret
  text untouched;
- ``secretsrc`` — every fetched value (all kinds, injected boundaries included)
  lands in the registry;
- the shared processor chain — a secret logged after registration reaches
  NEITHER the file JSONL nor stderr, and the mask reaches both.
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog
from shipit import execrun, logsetup, redact, secretsrc
from shipit.identity import repo_from_slug
from shipit.config import SecretSource

SECRET = "s3cr3t-hunter2-value"


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-lifetime by design; tests must not leak into
    each other (nor inherit whatever the suite registered earlier)."""
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


# ==========================================================================
# Exact-value masking — the registry
# ==========================================================================


def test_registered_value_is_masked():
    redact.register_secret(SECRET)
    assert redact.redact_text(f"token is {SECRET}, ok") == f"token is {redact.MASK}, ok"


def test_registered_value_masked_at_every_occurrence():
    redact.register_secret(SECRET)
    out = redact.redact_text(f"{SECRET} and again {SECRET}")
    assert SECRET not in out
    assert out.count(redact.MASK) == 2


def test_unregistered_value_passes_through():
    assert redact.redact_text(f"token is {SECRET}") == f"token is {SECRET}"


def test_overlapping_secrets_masked_longest_first():
    # A secret containing another registered secret as a substring must be
    # masked whole — no distinctive remainder left behind.
    redact.register_secret("abc")
    redact.register_secret("abc-def-ghi")
    assert redact.redact_text("x abc-def-ghi y") == f"x {redact.MASK} y"


def test_empty_and_none_registrations_are_ignored():
    redact.register_secret("")
    redact.register_secret(None)
    assert redact.redact_text("plain text") == "plain text"


def test_whitespace_only_registration_is_ignored():
    # Registering " " would replace every space in every record with the mask.
    redact.register_secret(" ")
    redact.register_secret("\t\n")
    assert redact.redact_text("plain text") == "plain text"


# ==========================================================================
# Pattern masking — GitHub tokens + PEM blocks
# ==========================================================================


@pytest.mark.parametrize(
    "token",
    [
        "ghp_abcDEF0123456789xyz",
        "gho_abcDEF0123456789xyz",
        "ghu_abcDEF0123456789xyz",
        "ghs_abcDEF0123456789xyz",
        "ghr_abcDEF0123456789xyz",
        "github_pat_11ABC_defGHI789",
    ],
)
def test_github_token_shapes_are_masked(token):
    out = redact.redact_text(f"auth with {token} failed")
    assert token not in out
    assert redact.MASK in out


def test_pem_block_is_masked_whole():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA7bq\nZm9vYmFy\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact.redact_text(f"key was:\n{pem}\ndone")
    assert "PRIVATE KEY" not in out
    assert "MIIEowIBAAKCAQEA7bq" not in out
    assert out == f"key was:\n{redact.MASK}\ndone"


def test_non_secret_text_is_untouched():
    text = "PR #231 merged; 3 files changed, branch LOG01/WS02, rc=0"
    assert redact.redact_text(text) == text


# ==========================================================================
# The processor — msg and extras, all value shapes
# ==========================================================================


def test_processor_masks_event_and_string_extras():
    redact.register_secret(SECRET)
    event = {"event": f"fetched {SECRET}", "detail": f"was {SECRET}", "pr": 231}
    out = redact.redact_event(None, "info", event)
    assert out["event"] == f"fetched {redact.MASK}"
    assert out["detail"] == f"was {redact.MASK}"
    assert out["pr"] == 231  # non-string scalar untouched, type preserved


def test_processor_masks_secret_riding_a_container():
    # A bound container would be stringified by a renderer downstream of the
    # redactor — so a secret inside one must degrade to a masked repr here.
    redact.register_secret(SECRET)
    event = {"event": "m", "args": ["ok", SECRET], "clean": [1, 2]}
    out = redact.redact_event(None, "info", event)
    assert SECRET not in str(out["args"])
    assert redact.MASK in out["args"]
    assert out["clean"] == [1, 2]  # clean container keeps its type


def test_processor_masks_secret_visible_only_via_str():
    # The human surface renders extras with str(), the file sink with repr();
    # an object whose repr is clean but whose str carries the secret must
    # still degrade — to its (clean) repr string — so neither renderer can
    # stringify the secret out of it downstream.
    class CleanReprDirtyStr:
        def __repr__(self) -> str:
            return "<opaque>"

        def __str__(self) -> str:
            return f"holds {SECRET}"

    redact.register_secret(SECRET)
    out = redact.redact_event(None, "info", {"event": "m", "obj": CleanReprDirtyStr()})
    assert isinstance(out["obj"], str)
    assert SECRET not in out["obj"]
    assert SECRET not in str(out["obj"])
    assert out["obj"] == "<opaque>"


# ==========================================================================
# secretsrc registers every fetched value
# ==========================================================================


def test_resolve_doppler_registers_fetched_value():
    source = SecretSource(name="TOK", kind="doppler", key="TOK")
    value = secretsrc.resolve(source, doppler_get=lambda key: SECRET)
    assert value == SECRET
    assert redact.redact_text(SECRET) == redact.MASK


def test_resolve_env_registers_fetched_value():
    source = SecretSource(name="TOK", kind="env", key="TOK")
    value = secretsrc.resolve(source, env={"TOK": SECRET})
    assert value == SECRET
    assert redact.redact_text(SECRET) == redact.MASK


def test_resolve_prompt_registers_fetched_value():
    source = SecretSource(name="TOK", kind="prompt", key=None)
    value = secretsrc.resolve(source, prompt=lambda name: SECRET)
    assert value == SECRET
    assert redact.redact_text(SECRET) == redact.MASK


def test_doppler_get_registers_directly(monkeypatch):
    # ghauth calls doppler_get without going through resolve(); that path must
    # register too. doppler_get fetches through the Exec seam (ADR-0028), so the
    # boundary faked here is execrun.run returning a completed ExecResult.
    result = execrun.ExecResult(
        argv=("doppler",), rc=0, stdout=SECRET + "\n", stderr="", duration_ms=1
    )
    monkeypatch.setattr(secretsrc.execrun, "run", lambda *a, **kw: result)
    assert secretsrc.doppler_get("TOK") == SECRET
    assert redact.redact_text(SECRET) == redact.MASK


def test_optional_miss_registers_nothing():
    source = SecretSource(name="TOK", kind="env", key="TOK", optional=True)
    assert secretsrc.resolve(source, env={}) is None
    assert redact.redact_text("some text") == "some text"


# ==========================================================================
# End to end — redaction applies to every sink via the shared chain
# ==========================================================================


@pytest.fixture()
def _reset_package_logger():
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    saved = list(logger.handlers)
    saved_level, saved_prop = logger.level, logger.propagate
    for handler in saved:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


def test_registered_secret_reaches_no_sink(capfd, tmp_path, _reset_package_logger):
    structlog.contextvars.clear_contextvars()
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    source = SecretSource(name="TOK", kind="env", key="TOK")
    secretsrc.resolve(source, env={"TOK": SECRET})

    logging.getLogger("shipit.ws02").warning(
        "fetched %s ok", SECRET, extra={"detail": f"used {SECRET}"}
    )

    logger = logging.getLogger(logsetup.LOGGER_NAME)
    for handler in logger.handlers:
        handler.flush()

    # File sink (JSONL): masked in msg AND extras, record still parses.
    lines = (tmp_path / "o" / "r" / "shipit.log").read_text().splitlines()
    (record,) = [json.loads(line) for line in lines if line]
    assert SECRET not in json.dumps(record)
    assert record["msg"] == f"fetched {redact.MASK} ok"
    assert record["detail"] == f"used {redact.MASK}"

    # Console sink (stderr): masked through the same chain.
    err = capfd.readouterr().err
    assert SECRET not in err
    assert redact.MASK in err


def test_pattern_masking_applies_through_the_chain(
    capfd, tmp_path, _reset_package_logger
):
    structlog.contextvars.clear_contextvars()
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    token = "ghp_abcDEF0123456789xyz"
    logging.getLogger("shipit.ws02").warning("auth with %s failed", token)

    logger = logging.getLogger(logsetup.LOGGER_NAME)
    for handler in logger.handlers:
        handler.flush()

    lines = (tmp_path / "o" / "r" / "shipit.log").read_text().splitlines()
    (record,) = [json.loads(line) for line in lines if line]
    assert token not in json.dumps(record)
    assert record["msg"] == f"auth with {redact.MASK} failed"
    err = capfd.readouterr().err
    assert token not in err
