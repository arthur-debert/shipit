"""The ``BackendAdapter`` seam: per-backend argv and auth-env.

See docs/adr/0020-backend-adapter-contract.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path


class BackendAdapter(ABC):
    """The per-backend launch seam; an instance may carry per-Run model/timeout config."""

    #: The ``--backend`` token; concrete adapters set it as a class attr.
    name: str

    #: The reasoning level actually wired into this adapter's argv — records stamp
    #: from HERE, never from config, so a level the backend cannot apply reads unset.
    reasoning: str | None = None

    @abstractmethod
    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The non-shell child argv; ``cwd`` and ``output_schema_path`` may be ignored."""

    @abstractmethod
    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """A fresh env dict: the parent's, with this backend's auth hazards scrubbed."""
