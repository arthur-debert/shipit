"""The pure text splicers — how a block unit lives inside a consumer-owned file.

String in, string out, no filesystem: marker blocks, pixi env-membership
merges, and settings.json hook entries. A file this module cannot edit
precisely is preserved verbatim, never clobbered.
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
    """Insert or replace the managed block; a first insert goes under ``anchor``, else at EOF."""
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


# The two sentinels below, and SETTINGS_MALFORMED, are read as
# present-but-divergent (OVERRIDE), so a file this module cannot edit precisely
# is surfaced for a human instead of re-proposed as an ADD forever.
ENV_MEMBER_MALFORMED = "\x00shipit-pixi-env-malformed\x00"

ENV_MEMBER_UNSUPPORTED = "\x00shipit-pixi-env-unsupported\x00"


def _env_features(spec: object) -> list[str] | None:
    """The feature list an ``[environments]`` entry composes — list or table form; ``None`` if neither."""
    feats = spec.get("features") if isinstance(spec, dict) else spec
    return [str(f) for f in feats] if isinstance(feats, list) else None


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_array(features: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(f) for f in features) + "]"


def _table_header(stripped_line: str) -> str:
    """A table-header line reduced to its bare ``[table]`` form, dropping any trailing comment."""
    close = stripped_line.find("]")
    return stripped_line[: close + 1] if close != -1 else stripped_line


def _value_end(text: str, start: int) -> int:
    """The offset past the balanced ``[...]``/``{...}`` value at ``start``, or ``start`` if it is neither."""
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
    return start


def _header_line_index(text: str, header: str) -> int | None:
    for idx, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if (
            stripped.startswith("[")
            and not stripped.startswith("[[")
            and _table_header(stripped) == header
        ):
            return idx
    return None


def _locate_env_assignment(text: str, env: str) -> tuple[int, int] | None:
    """The ``(value_start, value_end)`` offsets of ``env``'s inline value, or ``None`` if unlocatable."""
    lines = text.splitlines(keepends=True)
    in_table = False
    key = re.compile(
        rf"""^\s*(?:{re.escape(env)}|"{re.escape(env)}"|'{re.escape(env)}')\s*=\s*"""
    )
    offset = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            in_table = _table_header(stripped) == "[environments]"
        elif in_table:
            m = key.match(line)
            if m:
                value_start = offset + m.end()
                value_end = _value_end(text, value_start)
                return None if value_end == value_start else (value_start, value_end)
        offset += len(line)
    return None


def _insert_env_candidate(text: str, env: str, features: Sequence[str]) -> str:
    """``text`` with ``env = [features]`` inserted; never emits a second ``[environments]`` header."""
    entry = f"{env} = {_render_array(features)}"
    idx = _header_line_index(text, "[environments]")
    if idx is None:
        base = text.rstrip("\n")
        sep = "\n\n" if base else ""
        return f"{base}{sep}[environments]\n{entry}\n"
    lines = text.splitlines(keepends=True)
    if not lines[idx].endswith("\n"):
        lines[idx] += "\n"
    lines.insert(idx + 1, entry + "\n")
    return "".join(lines)


_TABLE_FEATURES_KEY = re.compile(r"(?<![\w.\-])features\s*=\s*(?=\[)")


def _feature_array_candidates(
    text: str, env: str, features: Sequence[str], *, is_table: bool
):
    """Each text in which ``env``'s feature array is rewritten — one per plausible key in table form."""
    located = _locate_env_assignment(text, env)
    if located is None:
        return
    value_start, value_end = located
    if not is_table:
        yield text[:value_start] + _render_array(features) + text[value_end:]
        return
    table = text[value_start:value_end]
    for m in _TABLE_FEATURES_KEY.finditer(table):
        array_start = value_start + m.end()
        array_end = _value_end(text, array_start)
        if array_end != array_start:
            yield text[:array_start] + _render_array(features) + text[array_end:]


def _verified(
    candidate: str, before: dict, env: str, target_spec: object
) -> str | None:
    """``candidate`` iff it re-parses to ``before`` with only ``environments[env]`` changed, else ``None``."""
    try:
        after = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError:
        return None
    environments = dict(before.get("environments") or {})
    environments[env] = target_spec
    expected = {**before, "environments": environments}
    return candidate if after == expected else None


def _plan_env_edit(
    text: str, env: str, required: Sequence[str], create_features: Sequence[str]
) -> str | None:
    """The verified edit making ``env`` compose every ``required`` feature, or ``None``; assumes ``text`` parses."""
    before = tomllib.loads(text) if text.strip() else {}
    environments = before.get("environments")
    if environments is not None and not isinstance(environments, dict):
        return None
    spec = environments.get(env) if isinstance(environments, dict) else None
    if spec is None:
        target = list(create_features)
        return _verified(_insert_env_candidate(text, env, target), before, env, target)
    if isinstance(spec, dict):
        features = spec.get("features")
        if not isinstance(features, list):
            return None
        current = [str(f) for f in features]
        target_features = current + [r for r in required if r not in current]
        target_spec: object = {**spec, "features": target_features}
    elif isinstance(spec, list):
        current = [str(f) for f in spec]
        target_features = current + [r for r in required if r not in current]
        target_spec = target_features
    else:
        return None
    for candidate in _feature_array_candidates(
        text, env, target_features, is_table=isinstance(spec, dict)
    ):
        verified = _verified(candidate, before, env, target_spec)
        if verified is not None:
            return verified
    return None


def _is_satisfied(spec: object, required: Sequence[str]) -> bool:
    features = _env_features(spec)
    return features is not None and all(r in features for r in required)


def extract_env_member(text: str, env: str, required: Sequence[str]) -> str | None:
    """The managed membership token when ``env`` composes every ``required`` feature.

    ``None`` when absent or editable (an ADD); :data:`ENV_MEMBER_UNSUPPORTED` when
    present in a form no verified edit exists for, :data:`ENV_MEMBER_MALFORMED`
    when the manifest does not parse — both read as present-but-divergent.
    """
    if not text.strip():
        return None
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ENV_MEMBER_MALFORMED
    environments = manifest.get("environments")
    spec = environments.get(env) if isinstance(environments, dict) else None
    if spec is not None and _is_satisfied(spec, required):
        return env_member_token(env, required)
    if _plan_env_edit(text, env, required, create_features=required) is None:
        return ENV_MEMBER_UNSUPPORTED
    return None


def splice_env_member(
    text: str, env: str, stock_line: str, required: Sequence[str]
) -> str:
    """Ensure ``env`` composes every ``required`` feature; creates it from ``stock_line`` when absent."""
    if not text.strip():
        manifest: dict = {}
    else:
        try:
            manifest = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return text
    stock_features = tomllib.loads(stock_line).get(env, [])
    environments = manifest.get("environments")
    spec = environments.get(env) if isinstance(environments, dict) else None
    if spec is not None and _is_satisfied(spec, required):
        return text
    edited = _plan_env_edit(text, env, required, create_features=stock_features)
    return text if edited is None else edited


def is_shipit_hook(entry: object, marker: str = SETTINGS_HOOK_MARKER) -> bool:
    """Whether a hooks-array entry is shipit's managed one; malformed structure answers ``False``."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and marker in str(h.get("command") or "") for h in hooks
    )


SETTINGS_MALFORMED = "\x00shipit-settings-malformed\x00"


def extract_settings_hook(
    text: str,
    event: str = EVENT_PRETOOLUSE,
    marker: str = SETTINGS_HOOK_MARKER,
) -> str | None:
    """shipit's current ``event`` entry in a settings.json text, canonical, or ``None``.

    ``None`` for an empty file or one with no shipit entry (an ADD);
    :data:`SETTINGS_MALFORMED` for unparseable JSON or a non-object, read as
    present-but-divergent. Only shipit's own ``event`` entry is inspected.
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
    """Merge shipit's ``event`` entry into a settings.json; a malformed or non-object file is returned verbatim."""
    stripped = text.strip()
    if stripped:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        if not isinstance(data, dict):
            return text
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


def is_retired_hook(entry: object, marker: str) -> bool:
    """Whether an entry is a retired consumer-local one — shipit's OWN managed entries are never retired."""
    return is_shipit_hook(entry, marker) and not is_shipit_hook(
        entry, MANAGED_HOOK_COMMAND_MARKER
    )


def count_retired_hooks(text: str, event: str, marker: str) -> int:
    """How many retired entries the ``event`` array carries; anything unreadable counts 0."""
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
    """Drop every retired entry from the ``event`` array; an emptied array (and ``hooks``) is dropped too."""
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
