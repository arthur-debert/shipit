"""``pixienv/run`` — the execution side of the pixi Tool adapter (ADR-0028).

Every ``pixi`` argv shipit *executes* (or hands to a launcher) is built HERE, in
pixi's domain home — beside the read side (:mod:`shipit.pixienv.read`) and the
activation model it drives (:mod:`shipit.pixienv.model`). Any pixi argv built
outside this package is a review defect (ADR-0028). Three pieces:

- :func:`install` — ``pixi install`` in a project root, carrying pixi's own
  long-runner timeout default (:data:`INSTALL_TIMEOUT`): a cold install
  (solve + download) is the legitimate long-runner ADR-0028 names, which the
  Exec runner's 5-minute default must not kill.
- :func:`run_argv` / :func:`run_in_env` — run-wrapping: re-express an argv as
  ``pixi run --manifest-path <root>/pixi.toml -- <argv>`` so the child resolves
  the project's OWN env (explicit ``--manifest-path`` overrides any leaked
  ``PIXI_PROJECT_MANIFEST``; the ``--`` separates pixi's args from the child's).
  ``run_argv`` is the pure builder (for launchers that execute through their own
  seam, e.g. :func:`shipit.spawn.launch.pixi_wrap`); ``run_in_env`` executes it.
- :func:`cache_dir` / :func:`has_default_env` — pixi's on-disk knowledge: where
  the package cache lives and whether a checkout carries a provisioned default
  env (the sentinel the launch-routing gate keys on).

The Exec boundary is injectable (``runner``, resolved to :func:`shipit.execrun.run`
at CALL time so a patched ``execrun.run`` is honored), so argv/env/timeout are
asserted without spawning a real ``pixi``. pixi stays a subprocess + JSON borrow
(ADR-0022) — never a rust-binding import.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import platformdirs

from .. import execrun

#: pixi's manifest file name — the project marker every wrap/gate keys on.
MANIFEST_NAME = "pixi.toml"

#: The provisioned-env sentinel: a checkout carries a usable pixi env iff this
#: directory exists under it (``pixi install`` materializes
#: ``<root>/.pixi/envs/default``). See :func:`has_default_env`.
DEFAULT_ENV_DIR = (".pixi", "envs", "default")

#: pixi's long-runner timeout, in seconds: 30 minutes. A cold ``pixi install``
#: (solve + download on a slow link) is the legitimate long-runner ADR-0028 names,
#: so the Exec runner's 5-minute default would kill it — but ``None`` would let a
#: wedged solve hang forever. 30 minutes is generous enough for a cold install,
#: tight enough that a wedged step still dies at a known bound with a durable
#: record. :func:`run_in_env` shares it: its worst case is a first activation
#: re-solving the env — provisioning-shaped work.
INSTALL_TIMEOUT: float = 30 * 60.0


def has_default_env(root: str | Path) -> bool:
    """Whether ``root`` carries a provisioned default pixi env (the routing sentinel).

    Pure (a filesystem ``is_dir`` probe only): ``pixi install`` materializes
    :data:`DEFAULT_ENV_DIR` as a directory, so its presence is the gate for
    "route this child through ``pixi run``" — absent (a reviewer's read-only
    Tree, ADR-0018, or a non-pixi repo), wrapping would force a solve into a
    chmod'd tree or fail outright. ``is_dir`` (not ``exists``) matches the
    sentinel's intent: a stray file at that path is not a provisioned env
    (agy review).
    """
    return Path(root).joinpath(*DEFAULT_ENV_DIR).is_dir()


def run_argv(argv: list[str], root: str | Path) -> list[str]:
    """``argv`` re-expressed to run THROUGH ``root``'s pixi env — the pure builder.

    ``pixi run --manifest-path <root>/pixi.toml -- <argv>``: the explicit
    ``--manifest-path`` overrides any leaked ``PIXI_PROJECT_MANIFEST`` so the child
    resolves ``root``'s OWN manifest, and the ``--`` separates pixi's args from the
    child argv. Pure argv-in/argv-out, for callers that execute through their own
    Exec seam (the backend launch contract, :mod:`shipit.spawn.launch`).
    """
    return [
        "pixi",
        "run",
        "--manifest-path",
        str(Path(root) / MANIFEST_NAME),
        "--",
        *argv,
    ]


def install(
    root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    runner=None,
) -> execrun.ExecResult:
    """Run ``pixi install`` in ``root`` and return the step's :class:`ExecResult`.

    ``env``, when given, is the COMPLETE child environment (``replace_env=True``) —
    callers hand in a scrubbed snapshot (:func:`shipit.pixienv.scrub_env`) so a
    parent's leaked project pointers cannot creep back in via a merge over
    ``os.environ``; ``None`` inherits the current environment unchanged. The Exec
    carries pixi's own long-runner bound (:data:`INSTALL_TIMEOUT`) and raises
    :class:`~shipit.execrun.ExecError` on failure like any checked Exec — the runner's
    durable ERROR record carries both stream tails, which is where a broken install
    writes its real diagnostics.
    """
    if runner is None:
        runner = execrun.run
    return runner(
        ["pixi", "install"],
        cwd=str(root),
        env=None if env is None else dict(env),
        replace_env=env is not None,
        timeout=INSTALL_TIMEOUT,
    )


def run_in_env(
    argv: list[str],
    root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = INSTALL_TIMEOUT,
    runner=None,
) -> execrun.ExecResult:
    """Execute ``argv`` through ``root``'s pixi env (:func:`run_argv`), one Exec.

    ``env`` follows :func:`install`'s contract (complete child env when given,
    inherit when ``None``); ``check=False`` makes a nonzero child a normal
    :class:`ExecResult` for probe-shaped callers. The default ``timeout`` is
    pixi's long-runner bound: a ``pixi run``'s worst case is a first activation
    re-solving the env — provisioning-shaped work, so it shares provisioning's
    bound rather than the runner's 5-minute default.
    """
    if runner is None:
        runner = execrun.run
    return runner(
        run_argv(argv, root),
        cwd=str(root),
        env=None if env is None else dict(env),
        replace_env=env is not None,
        check=check,
        timeout=timeout,
    )


def cache_dir() -> Path:
    """The directory pixi/rattler caches downloaded packages in.

    Honors ``PIXI_CACHE_DIR`` / ``RATTLER_CACHE_DIR`` overrides, else the platform
    default (``<user-cache>/rattler/cache``) — the same location ``pixi info``
    reports as ``cache_dir``, computed pure (env + platformdirs) so warn-only
    callers (the #119 same-filesystem check) never need a subprocess to answer it.
    """
    override = os.environ.get("PIXI_CACHE_DIR") or os.environ.get("RATTLER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("rattler")) / "cache"
