"""Windows import safety for the CLI entry chain (#893).

shipit's first live windows build leg died at IMPORT time: the verbs import
chain (``shipit.cli`` → ``verbs.build`` → ``verbs._errors`` → prstate → review
→ ``harness.eval.store``) transitively reached a module-level ``import fcntl``
— unix-only, absent on win32 — so ``shipit build`` crashed on the windows
runner before parsing a single argument. The fix defers ``fcntl`` into the
store family's locking seam (:func:`shipit.harness.eval.store.lock_exclusive`
/ :func:`~shipit.harness.eval.store.lock_shared`, a documented no-op on
Windows); this file is the guard that the regression class stays fixed:

- the FULL CLI entry chain (``shipit.cli``, the ``shipit`` console script's
  module — every verb, including the build/bundle path that runs on release
  runners) imports cleanly with the unix-only stdlib modules masked, so the
  next module-level ``import fcntl`` (or ``pwd``/``grp``/``termios``/
  ``resource``) on any verb's chain fails HERE, not on a windows runner;
- the locking seam itself is RUNNABLE under the win32 guard: a store
  append/read round-trips with ``sys.platform`` reporting ``win32``.
"""

from __future__ import annotations

import subprocess
import sys

from shipit.harness.eval import store
from shipit.identity import Owner, Repo

_UNIX_ONLY = ("fcntl", "pwd", "grp", "termios", "resource")

_IMPORT_UNDER_MASK = f"""
import importlib.abc
import sys

MASKED = {_UNIX_ONLY!r}


class Win32Mask(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in MASKED:
            raise ModuleNotFoundError(
                f"No module named {{fullname!r}} (masked: unix-only, absent on win32)",
                name=fullname,
            )
        return None


# Interpreter startup (site) may have imported some of these already (pwd,
# typically); evict them so every import BELOW — the chain under test —
# resolves through the mask, exactly as on a win32 interpreter.
for name in MASKED:
    sys.modules.pop(name, None)
sys.meta_path.insert(0, Win32Mask())

import shipit.cli  # noqa: F401  — the console entry chain: every verb group.

# Prove the mask was live, not vacuously inert.
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("mask inert: fcntl imported cleanly")
print("ok")
"""


def test_cli_entry_chain_imports_with_unix_only_modules_masked():
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_UNDER_MASK],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_store_locking_seam_is_a_runnable_no_op_on_win32(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    repo = Repo(owner=Owner(login="acme"), name="widget")
    store.append_record({"a": 1}, repo, base_dir=tmp_path / "state")
    assert store.read_records(repo, base_dir=tmp_path / "state") == [{"a": 1}]
