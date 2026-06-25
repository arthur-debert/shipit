"""base — the review-backend interface.

A `Backend` wraps one agent CLI (codex, agy). The interface separates three
concerns so `--dry-run` is honest:

  * ``preflight()`` probes that the agent binary is reachable and raises a
    clear, actionable :class:`BackendUnavailable` if not (it never auto-starts
    anything);
  * ``build_command()`` returns a pure description of what *would* run — argv,
    stdin, and any temp files (by placeholder path) — which is exactly what
    ``--dry-run`` prints;
  * ``run()`` actually executes it: writes the temp files, invokes the CLI via
    the shared ``proc`` helper, parses stdout via ``extract_json``, and cleans
    up the temp files in a ``finally`` (mirroring the phos scripts).
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod

#: How much of the raw agent output to echo back in a parse-failure message —
#: enough to see the head and tail (where a truncation marker lives) without
#: dumping a whole review into the terminal.
_SNIPPET = 200
#: agy prints this when its ``--print`` timeout fires mid-response; the output
#: is then a TRUNCATED JSON object followed by the marker, which ``extract_json``
#: can't parse. Detecting it lets the error say "timed out" explicitly.
_TIMEOUT_MARKER = "timed out waiting for response"


class BackendUnavailable(RuntimeError):
    """The backend's agent binary is not reachable — message tells the user how
    to remediate (install / start the agent). Raised by ``preflight``."""


class BackendError(RuntimeError):
    """A backend ran but produced output we couldn't turn into a review.

    Raised when ``extract_json`` can't parse the agent's stdout (truncated /
    non-JSON output — commonly an agent timeout). Subclasses ``RuntimeError``
    so ``_LocalReviewAdapter.request`` already normalizes it to ``GhError``
    (clean error + exit 1, never a raw traceback)."""


def parse_review_output(stdout: str, *, backend_name: str = "the agent") -> dict:
    """Parse an agent's stdout into a review dict, or raise :class:`BackendError`.

    Wraps :func:`shipit.review.schema.extract_json` (which still raises a
    bare ``ValueError`` on unparseable input) at the backend boundary, turning
    that into an actionable :class:`BackendError`: it includes a head/tail
    snippet of the raw output for debugging and, when the agent's timeout marker
    is present, says so explicitly so the user knows to use a faster model or a
    smaller diff.

    ``backend_name`` names the calling backend (e.g. ``"codex"`` / ``"agy"``) so
    the timeout hint blames the RIGHT backend — this function is shared by every
    backend, so a hardcoded name would mislabel a different backend's timeout.
    """
    # Local import: schema is a sibling, but keeping it here avoids any chance
    # of an import-order issue and matches the lazy style used elsewhere.
    from ..schema import extract_json

    try:
        return extract_json(stdout)
    except ValueError as exc:
        raw = stdout or ""
        if _TIMEOUT_MARKER in raw.lower():
            hint = (
                f"{backend_name} timed out before returning a complete review — "
                "try a faster model or a smaller diff"
            )
        else:
            hint = (
                "the agent returned no parseable JSON (it may have timed out or "
                "been truncated) — try a faster model or a smaller diff"
            )
        snippet = (
            f"{raw[:_SNIPPET]} … {raw[-_SNIPPET:]}" if len(raw) > 2 * _SNIPPET else raw
        )
        raise BackendError(f"{hint}\nraw output: {snippet}") from exc


class Backend(ABC):
    """Abstract review backend. One concrete subclass per agent CLI."""

    #: Short backend identifier, e.g. ``"codex"`` / ``"agy"``.
    name: str = ""

    #: Name of the agent binary that must be on PATH for this backend to run.
    binary: str = ""

    def preflight(self) -> None:
        """Verify the agent binary is reachable; raise :class:`BackendUnavailable`
        with an actionable message otherwise. Does NOT auto-start anything."""
        if shutil.which(self.binary) is None:
            raise BackendUnavailable(
                f"The '{self.name}' review backend requires the '{self.binary}' "
                f"CLI on your PATH, but it was not found. Install it (and start "
                f"its backend if it needs one), then re-run."
            )

    @abstractmethod
    def build_command(self, prompt: str, schema: dict) -> dict:
        """Describe — without executing — exactly what would run.

        Returns ``{"argv": [...], "stdin": <str|None>, "files": {path: contents}}``
        where ``files`` are any temp files that would be written (shown by a
        placeholder path). This is what ``--dry-run`` prints.
        """
        raise NotImplementedError

    @abstractmethod
    def run(self, prompt: str, schema: dict, *, cwd: str | None = None) -> dict:
        """Execute the backend for real and return the parsed review dict.

        Writes any temp files, invokes the CLI (in ``cwd`` if given, so the
        read-only agent can inspect the checkout's files), parses stdout via
        ``extract_json``, and removes the temp files in a ``finally``.
        """
        raise NotImplementedError
