"""The pure text splicers — how a block unit lives inside a consumer-owned file.

Two variants, both string-in/string-out with no filesystem:

- **marker blocks** (:func:`extract_block` / :func:`splice_block`) — shipit's
  region fenced by an open/close comment-marker pair, optionally anchored under
  a TOML table header on first insert (the AGENTS.md and pixi.toml units).
- **JSON hooks** (:func:`extract_settings_hook` / :func:`splice_settings_hook`)
  — shipit's one entry in a ``settings.json`` hooks-event array, identified by
  its command marker; every other key the consumer set merges through
  untouched, and a malformed file is preserved verbatim (a conflict to surface,
  never a clobber or a crash).

The RETIRED-hooks pair (:func:`count_retired_hooks` / :func:`remove_retired_hooks`,
#619) rides the same JSON walk in the opposite direction: consumer-local hook
entries shipit used to prescribe (the legacy ``bin/install-release-core``
resolver, the pre-managed ``setup-dev-env.sh`` duplicate) are dropped from
their event array, with shipit's OWN managed entries protected by their
``shipit hook`` command marker.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Sequence

from .units import (
    BLOCK_CLOSE,
    BLOCK_OPEN,
    EVENT_PRETOOLUSE,
    MANAGED_HOOK_COMMAND_MARKER,
    SETTINGS_HOOK_MARKER,
    canonical_hook_entry,
    env_member_token,
)


def extract_block(
    text: str, open_marker: str = BLOCK_OPEN, close_marker: str = BLOCK_CLOSE
) -> str | None:
    """The inner text of the marker-delimited block, or ``None`` when absent."""
    i = text.find(open_marker)
    if i == -1:
        return None
    j = text.find(close_marker, i)
    if j == -1:
        return None
    return text[i + len(open_marker) : j].strip("\n")


def splice_block(
    text: str,
    inner: str,
    open_marker: str = BLOCK_OPEN,
    close_marker: str = BLOCK_CLOSE,
    anchor: str | None = None,
) -> str:
    """Insert or replace the managed block in ``text``, owning only the block.

    When the markers are already present the block is replaced in place. On a
    first insert with an ``anchor`` (a TOML table header), the block is placed
    immediately after that header — creating the header at EOF if absent — so the
    managed keys land inside the right table. Without an anchor it appends at EOF
    (the AGENTS.md case).
    """
    block = f"{open_marker}\n{inner}\n{close_marker}"
    i = text.find(open_marker)
    if i != -1:
        j = text.find(close_marker, i)
        if j != -1:
            return text[:i] + block + text[j + len(close_marker) :]
    if anchor is not None:
        return _insert_under_anchor(text, anchor, block)
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}\n{block}\n" if text else f"{block}\n"


def _insert_under_anchor(text: str, anchor: str, block: str) -> str:
    """Place ``block`` right after the ``anchor`` line, adding the anchor if absent."""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == anchor:
            spliced = lines[: idx + 1] + block.splitlines() + lines[idx + 1 :]
            return "\n".join(spliced) + "\n"
    base = text.rstrip("\n")
    sep = "\n\n" if base else ""
    return f"{base}{sep}{anchor}\n{block}\n"


# --------------------------------------------------------------------------
# Environment-membership merge — pixi `[environments]` (the FMT_ENV_MEMBER variant)
#
# The lint env must COMPOSE the managed `shipit-lexd` feature (ADR-0066: the
# fleet-uniform lexd gate), but WHICH base features the env carries is the
# consumer's own config (ADR-0047). A plain marker block cannot express that: it
# owns the whole `lint = [...]` line, so a consumer who already declares
# `[environments] lint` collides on the key and the block is skipped — leaving
# lexd unwired (lint breaks with no `provision` fallback). This variant owns ONLY
# `shipit-lexd`'s MEMBERSHIP in the env's feature list — like the JSON-hook
# variant owns just shipit's one entry in a consumer-owned hooks array — so the
# consumer's other features merge through untouched and the merge is idempotent.
# --------------------------------------------------------------------------

#: Sentinel inner for a pixi.toml that exists but is unparseable — read as
#: present-but-divergent (OVERRIDE, surfaced for a human), and the write preserves
#: it verbatim. Mirrors :data:`SETTINGS_MALFORMED` for the JSON-hook variant.
ENV_MEMBER_MALFORMED = "\x00shipit-pixi-env-malformed\x00"


def _env_features(spec: object) -> list[str] | None:
    """The feature list an ``[environments]`` entry composes, or ``None``.

    pixi accepts either a bare list (``lint = ["lint"]``) or a table
    (``lint = { features = ["lint"] }``); both forms yield the list here. A form
    this splicer does not edit (a non-list ``features``) yields ``None``.
    """
    feats = spec.get("features") if isinstance(spec, dict) else spec
    return [str(f) for f in feats] if isinstance(feats, list) else None


def extract_env_member(text: str, env: str, required: Sequence[str]) -> str | None:
    """The managed membership token when ``env`` composes every ``required`` feature.

    In lockstep with :func:`splice_env_member`, three outcomes:

      - the ``env`` entry composes all ``required`` features -> the canonical
        token (NOOP by hash — the managed invariant already holds).
      - empty file, no ``[environments]`` table, no ``env`` entry, or an ``env``
        that is MISSING a required feature -> ``None`` ("absent" -> ADD; the write
        creates the entry or merges the missing feature into it).
      - **unparseable pixi.toml** -> a non-``None`` sentinel read as
        present-but-divergent (OVERRIDE): a malformed manifest is a CONFLICT to
        surface, never a file the reconcile silently rewrites.
    """
    if not text.strip():
        return None
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ENV_MEMBER_MALFORMED
    environments = manifest.get("environments")
    spec = environments.get(env) if isinstance(environments, dict) else None
    if spec is None:
        return None
    features = _env_features(spec)
    if features is None or any(r not in features for r in required):
        return None
    return env_member_token(env, required)


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string (the only escaping a feature name needs)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_features(features: Sequence[str], as_table: bool) -> str:
    """Render a feature list back in the form the consumer used (list or table)."""
    array = "[" + ", ".join(_toml_string(f) for f in features) + "]"
    return f"{{ features = {array} }}" if as_table else array


def _value_end(text: str, start: int) -> int:
    """The offset just past the (possibly multi-line) inline value at ``start``.

    Walks one balanced ``[...]``/``{...}`` value, honoring quoted strings so a
    bracket inside a string never miscounts. Returns ``start`` unchanged when the
    value is not a bracketed inline value (a form this splicer leaves alone).
    """
    if start >= len(text) or text[start] not in "[{":
        return start
    depth = 0
    quote: str | None = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return start  # unbalanced — leave the value untouched


def _replace_env_features(
    text: str, env: str, features: Sequence[str], as_table: bool
) -> str | None:
    """Rewrite ``env``'s feature list under ``[environments]`` in place.

    Preserves everything but the one value it rewrites. Returns ``None`` when the
    assignment is not the inline form this splicer edits (so the caller can leave
    the file untouched rather than corrupt it)."""
    lines = text.splitlines(keepends=True)
    in_table = False
    key = re.compile(
        rf"""^\s*(?:{re.escape(env)}|"{re.escape(env)}"|'{re.escape(env)}')\s*=\s*"""
    )
    offset = 0
    for line in lines:
        header = line.strip()
        if header.startswith("[") and not header.startswith("[["):
            in_table = header == "[environments]"
        elif in_table:
            m = key.match(line)
            if m:
                value_start = offset + m.end()
                value_end = _value_end(text, value_start)
                if value_end == value_start:
                    return None  # not a bracketed inline value — do not edit
                return (
                    text[:value_start]
                    + _render_features(features, as_table)
                    + text[value_end:]
                )
        offset += len(line)
    return None


def splice_env_member(
    text: str, env: str, stock_line: str, required: Sequence[str]
) -> str:
    """Ensure ``env`` composes every ``required`` feature, merging (never replacing).

    Owns ONLY the ``required`` features' membership in ``env``:

      - no ``env`` entry yet -> create it from ``stock_line`` (the packaged
        default, e.g. ``lint = ["lint", "shipit-lexd"]``) under ``[environments]``.
      - ``env`` exists and already composes every ``required`` feature -> return
        unchanged (idempotent NOOP).
      - ``env`` exists but MISSES a required feature -> append the missing one(s)
        to the consumer's own list, preserving their other features.

    Fail-safe like :func:`splice_settings_hook`: an unparseable manifest, or an
    ``env`` in a form this splicer does not edit, is returned verbatim (the read
    path classified the first as an OVERRIDE conflict; the second stays the
    consumer's own).
    """
    stripped = text.strip()
    if stripped:
        try:
            manifest = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return text  # malformed → preserve, never clobber (conflict surfaced)
    else:
        manifest = {}
    stock_features = tomllib.loads(stock_line).get(env, [])
    environments = manifest.get("environments")
    spec = environments.get(env) if isinstance(environments, dict) else None
    if spec is None:
        line = f"{env} = {_render_features(stock_features, as_table=False)}"
        return _insert_under_anchor(text, "[environments]", line)
    features = _env_features(spec)
    if features is None:
        return text  # a form this splicer does not edit → leave it the consumer's own
    missing = [r for r in required if r not in features]
    if not missing:
        return text  # the managed invariant already holds — idempotent NOOP
    merged = list(features) + missing
    replaced = _replace_env_features(text, env, merged, as_table=isinstance(spec, dict))
    return text if replaced is None else replaced


# --------------------------------------------------------------------------
# JSON-hook splicing — settings.json hook entries (the FMT_JSON_HOOK variant)
# --------------------------------------------------------------------------


def is_shipit_hook(entry: object, marker: str = SETTINGS_HOOK_MARKER) -> bool:
    """Whether a hooks-array entry is shipit's managed one (by command ``marker``).

    Defensive against a malformed consumer file: a non-dict entry, a ``hooks`` that
    is ``null`` or any non-list, a non-dict hook, or a hook whose ``command`` is
    ``null``/non-string all answer ``False`` ("not a shipit hook") rather than
    raising — the structure walk never trips on garbage.
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and marker in str(h.get("command") or "") for h in hooks
    )


#: Sentinel inner value for a settings.json that exists but is malformed/unparseable
#: or is not a JSON object. It is NOT a real hook entry — the read path returns it so
#: the unit hashes to something present-but-non-matching (→ OVERRIDE, surfaced for a
#: human), and the write path recognizes it to preserve the original byte-for-byte.
SETTINGS_MALFORMED = "\x00shipit-settings-malformed\x00"


def extract_settings_hook(
    text: str,
    event: str = EVENT_PRETOOLUSE,
    marker: str = SETTINGS_HOOK_MARKER,
) -> str | None:
    """shipit's current ``event`` entry in a settings.json text, canonical, or ``None``.

    Three outcomes, kept in lockstep with :func:`splice_settings_hook` so a read that
    classifies the file is honored by the write that follows:

      - empty file, or a JSON object with no shipit entry -> ``None`` ("absent" -> ADD;
        the write splices shipit's entry into the consumer's object, untouched).
      - a JSON object carrying shipit's entry -> the canonical entry (NOOP/UPDATE/
        OVERRIDE by hash, exactly as before).
      - **unparseable, or valid JSON that is not an object** -> a non-``None`` sentinel
        so the reconciler reads it as present-but-divergent (OVERRIDE): a malformed
        ``.claude/settings.json`` is a CONFLICT to surface for a human, never an
        absent file we ADD onto and never a crash. The matching write preserves it.

    Only shipit's own ``event`` entry (matched by ``marker``) is the managed region;
    the consumer's other settings — and shipit's entries in OTHER event arrays — are
    never inspected.
    """
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return SETTINGS_MALFORMED
    if not isinstance(data, dict):
        return SETTINGS_MALFORMED
    hooks = data.get("hooks")
    entries = hooks.get(event, []) if isinstance(hooks, dict) else []
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if is_shipit_hook(entry, marker):
            return canonical_hook_entry(entry)
    return None


def splice_settings_hook(
    text: str,
    inner: str,
    event: str = EVENT_PRETOOLUSE,
    marker: str = SETTINGS_HOOK_MARKER,
) -> str:
    """Merge shipit's ``event`` entry (``inner``, canonical JSON) into a settings.json.

    Owns ONLY shipit's entry in the ``event`` array: any prior shipit entry there
    (matched by ``marker``) is replaced, every other key and hook the consumer set —
    including shipit's entries in other event arrays — is preserved, and the file is
    returned as pretty-printed JSON. An empty/whitespace input starts from ``{}``.

    Fail-safe, matching :func:`extract_settings_hook`: a consumer file that is
    unparseable or is not a JSON object is NEVER clobbered — the original ``text`` is
    returned verbatim (the read path already classified it as an OVERRIDE conflict, so
    the install surfaces it for a human instead of overwriting or crashing).
    """
    stripped = text.strip()
    if stripped:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return text  # malformed → preserve, never clobber (conflict surfaced)
        if not isinstance(data, dict):
            return text  # not a JSON object → preserve, never clobber
    else:
        data = {}
    entry = json.loads(inner)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    current = hooks.get(event, [])
    if not isinstance(current, list):
        current = []
    hooks[event] = [e for e in current if not is_shipit_hook(e, marker)] + [entry]
    return json.dumps(data, indent=2) + "\n"


# --------------------------------------------------------------------------
# Retired hook entries (#619) — consumer-local entries shipit removes
# --------------------------------------------------------------------------


def is_retired_hook(entry: object, marker: str) -> bool:
    """Whether a hooks-array entry is a RETIRED consumer-local one (#619).

    Matched like :func:`is_shipit_hook` — some hook's command carries ``marker``
    — with one protection: an entry that is shipit's OWN managed one (its
    command carries :data:`~shipit.install.units.MANAGED_HOOK_COMMAND_MARKER`,
    the ``shipit hook <verb>`` form every managed entry runs) is NEVER retired.
    The managed SessionStart command itself runs ``./bin/setup-dev-env.sh``
    inline, so without the protection the setup-dev-env retirement marker would
    delete the very entry shipit manages.
    """
    return is_shipit_hook(entry, marker) and not is_shipit_hook(
        entry, MANAGED_HOOK_COMMAND_MARKER
    )


def count_retired_hooks(text: str, event: str, marker: str) -> int:
    """How many retired consumer-local entries ``text``'s ``event`` array carries.

    The read half of the retired-hooks pass (#619) — what gather feeds the
    two-case decision. A missing/empty/malformed/non-object file, or an event
    array that is not a list, counts 0: fail open, in lockstep with
    :func:`remove_retired_hooks` (which preserves such a file verbatim), so the
    pass never decides work the write cannot safely do.
    """
    text = text.strip()
    if not text:
        return 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    hooks = data.get("hooks")
    entries = hooks.get(event, []) if isinstance(hooks, dict) else []
    if not isinstance(entries, list):
        return 0
    return sum(1 for e in entries if is_retired_hook(e, marker))


def remove_retired_hooks(text: str, event: str, marker: str) -> str:
    """Drop every retired consumer-local entry from the ``event`` array (#619).

    The write half of the retired-hooks pass. Owns ONLY the matched entries:
    every other key and hook the consumer set — shipit's own managed entries
    included (protected in :func:`is_retired_hook`) — merges through untouched.
    An emptied ``event`` array (and an emptied ``hooks`` object) is dropped
    rather than left as litter. Fail-safe like :func:`splice_settings_hook`: a
    malformed or non-object file — and a file with nothing to remove — is
    returned verbatim, never clobbered or reformatted.
    """
    stripped = text.strip()
    if not stripped:
        return text
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return text
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return text
    kept = [e for e in entries if not is_retired_hook(e, marker)]
    if len(kept) == len(entries):
        return text
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]
        if not hooks:
            del data["hooks"]
    return json.dumps(data, indent=2) + "\n"
